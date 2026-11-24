"""DynamoDB persistence for report and claim records in the single-instance lab."""

import argparse
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class RecordsConflict(sqlite3.IntegrityError):
    """The record set changed before the write could commit."""


def rows_by_key(db):
    records = {}
    for table, kind in (('reports', 'REPORT'), ('claims', 'CLAIM')):
        for row in db.execute(f'SELECT * FROM {table}'):
            records[(kind, str(row['id']))] = dict(row)
    return records


class DynamoRecords:
    """Keep DynamoDB authoritative and use a transient SQLite view for existing queries."""

    state_key = {'pk': 'META', 'sk': 'records'}

    def __init__(self, name):
        import boto3
        from boto3.dynamodb.types import TypeSerializer

        self.table = boto3.resource('dynamodb').Table(name)
        self.client = boto3.client('dynamodb')
        self.serialize = TypeSerializer().serialize

    def attributes(self, item):
        return {key: self.serialize(value) for key, value in item.items()}

    def version(self):
        item = self.table.get_item(Key=self.state_key, ConsistentRead=True).get('Item')
        return int(item['version']) if item else None

    def snapshot(self):
        for _ in range(4):
            version = self.version()
            if version is None:
                raise RuntimeError('Run the reports migration before enabling DynamoDB storage.')
            records = {}
            for kind in ('REPORT', 'CLAIM'):
                query = dict(KeyConditionExpression='pk = :kind',
                             ExpressionAttributeValues={':kind': kind}, ConsistentRead=True)
                while True:
                    page = self.table.query(**query)
                    for item in page['Items']:
                        payload = item['record']
                        for field in ('id', 'owner_id', 'account_id', 'lost_id', 'found_id', 'withdrawn'):
                            if payload.get(field) is not None:
                                payload[field] = int(payload[field])
                        records[(kind, item['sk'])] = payload
                    if not page.get('LastEvaluatedKey'):
                        break
                    query['ExclusiveStartKey'] = page['LastEvaluatedKey']
            if version == self.version():
                return version, records
        raise RecordsConflict('Reports changed while loading. Try again.')

    def commit(self, version, previous, records):
        changed = {key: item for key, item in records.items() if previous.get(key) != item}
        removed = previous.keys() - records.keys()
        if version is not None and not changed and not removed:
            return
        state = {'TableName': self.table.name,
                 'Item': self.attributes({**self.state_key, 'version': 1 if version is None else version + 1})}
        if version is None:
            state['ConditionExpression'] = 'attribute_not_exists(pk)'
        else:
            state.update(ConditionExpression='#version = :version',
                         ExpressionAttributeNames={'#version': 'version'},
                         ExpressionAttributeValues={':version': self.serialize(version)})
        actions = [{'Put': state}]
        for (kind, key), record in changed.items():
            put = {'TableName': self.table.name,
                   'Item': self.attributes({'pk': kind, 'sk': key, 'record': record})}
            if version is None:
                put['ConditionExpression'] = 'attribute_not_exists(pk)'
            actions.append({'Put': put})
        for kind, key in removed:
            actions.append({'Delete': {'TableName': self.table.name,
                                       'Key': self.attributes({'pk': kind, 'sk': key})}})
        if len(actions) > 100:
            raise ValueError('This lab deployment supports up to 99 record changes in one transaction.')
        try:
            self.client.transact_write_items(TransactItems=actions)
        except self.client.exceptions.TransactionCanceledException as exc:
            reasons = {reason.get('Code') for reason in exc.response.get('CancellationReasons', [])}
            if reasons.intersection({'ConditionalCheckFailed', 'TransactionConflict'}):
                raise RecordsConflict('Reports changed before the operation committed.') from exc
            raise

    @contextmanager
    def connect(self, local_connection):
        version, records = self.snapshot()
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys = ON')
            with local_connection() as local:
                schema = local.execute("""
                    SELECT sql FROM sqlite_master WHERE tbl_name IN ('accounts','reports','claims')
                    AND sql IS NOT NULL ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END
                """).fetchall()
                for definition in schema:
                    db.execute(definition['sql'])
                db.executemany("INSERT INTO accounts (id, username, password_hash, display_name) VALUES (?, ?, '', ?)",
                               local.execute('SELECT id, username, display_name FROM accounts'))
            for kind, table in (('REPORT', 'reports'), ('CLAIM', 'claims')):
                columns = [row['name'] for row in db.execute(f'PRAGMA table_info({table})')]
                db.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                               ([record[column] for column in columns] for key, record in records.items() if key[0] == kind))
            db.commit()
            changes_before = db.total_changes
            with db:
                yield db
                if db.total_changes != changes_before:
                    self.commit(version, records, rows_by_key(db))


def migrate():
    parser = argparse.ArgumentParser()
    parser.add_argument('database', type=Path)
    parser.add_argument('table')
    args = parser.parse_args()
    with closing(sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        records = rows_by_key(db)
    store = DynamoRecords(args.table)
    if store.version() is None:
        store.commit(None, {}, records)
    _, saved = store.snapshot()
    if saved != records:
        raise SystemExit('DynamoDB records differ from SQLite. No existing records were overwritten.')
    print('Verified DynamoDB migration:', sum(key[0] == 'REPORT' for key in records), 'reports and',
          sum(key[0] == 'CLAIM' for key in records), 'claims.')


if __name__ == '__main__':
    migrate()
