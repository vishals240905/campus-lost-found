import os
import sqlite3
import re
from hmac import compare_digest
from secrets import token_hex
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from flask import Flask, Response, abort, g, redirect, render_template, request, send_from_directory, session, url_for
from PIL import Image
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

CATEGORIES = ('Accessories', 'Books & stationery', 'Electronics', 'ID & keys', 'Bags', 'Other')
LOCATIONS = ('Central Library', 'Academic Block', 'Cafeteria', 'Sports Complex', 'Student Centre', 'Other')
LOCKED_REPORT = """(reports.withdrawn = 1 OR EXISTS (
    SELECT 1 FROM claims WHERE status IN ('approved', 'returned')
    AND (lost_id = reports.id OR found_id = reports.id)
))"""
REPORTS_WITH_STATUS = """SELECT reports.*, CASE WHEN withdrawn = 1 THEN 'withdrawn' ELSE COALESCE((
    SELECT CASE status WHEN 'approved' THEN 'reserved' ELSE status END
    FROM claims WHERE status IN ('approved', 'returned')
    AND (lost_id = reports.id OR found_id = reports.id)
    LIMIT 1
), 'open') END AS report_status FROM reports"""
REPORT_ENDPOINTS = {'browse', 'detail', 'compare', 'photo', 'report', 'my_reports', 'withdraw_report'}
CLAIM_LABELS = {
    'pending': 'Pending review', 'approved': 'Awaiting handover', 'returned': 'Returned',
    'rejected': 'Rejected', 'cancelled': 'Approval cancelled', 'superseded': 'Closed after another return',
    'withdrawn': 'Report deleted',
}
CLAIM_STEPS = {
    'pending': 'Campus staff will review your private ownership detail.',
    'approved': 'Contact campus staff to arrange the handover.',
    'returned': 'The handover is complete.',
    'rejected': 'Campus staff declined this claim. Check other open matches.',
    'cancelled': 'The handover was cancelled. Check for other open matches.',
    'superseded': 'One of the reports was closed after a different claim completed.',
    'withdrawn': 'The person who posted one of the reports deleted it.',
}


def match_reasons(lost, found):
    if lost['category'] != found['category']:
        return []
    gap = (date.fromisoformat(found['day']) - date.fromisoformat(lost['day'])).days
    if not 0 <= gap <= 7:
        return []
    timing = {0: 'Lost and found on the same day', 1: 'Found one day after it was lost'}
    reasons = ['Same category', timing.get(gap, f'Found {gap} days after it was lost')]
    if lost['colour'].casefold() == found['colour'].casefold():
        reasons.append('Same colour')
    if lost['location'] == found['location']:
        reasons.append('Same campus location')
    def words(item):
        return set(re.findall(r'[^\W\d_]{3,}', (item['name'] + ' ' + item['description']).casefold()))
    common = words(lost) & words(found) - {'the', 'and', 'near', 'from', 'lost', 'found', 'with', 'was', 'this', 'that'}
    if common:
        reasons.append('Shared keywords: ' + ', '.join(sorted(common)[:4]))
    return reasons if len(reasons) >= 4 else []


def create_app(data_dir=None):
    app = Flask(__name__)
    photo_limit = 6 * 1024 * 1024
    app.config.update(MAX_CONTENT_LENGTH=photo_limit + 128 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('CAMPUS_SECURE_COOKIES') == '1')
    app.jinja_env.globals.update(claim_labels=CLAIM_LABELS, claim_steps=CLAIM_STEPS,
                                 report_labels={'open': 'Open', 'reserved': 'Awaiting handover', 'returned': 'Returned',
                                                'withdrawn': 'Deleted'})
    data = Path(data_dir or os.environ.get('CAMPUS_DATA_DIR') or app.instance_path).resolve()
    uploads = data / 'uploads'
    uploads.mkdir(parents=True, exist_ok=True)
    bucket = os.environ.get('CAMPUS_PHOTO_BUCKET')
    s3 = None
    if bucket:
        import boto3
        s3 = boto3.client('s3')
    accounts_table_name = os.environ.get('CAMPUS_ACCOUNTS_TABLE')
    accounts_table = None
    if accounts_table_name:
        import boto3
        accounts_table = boto3.resource('dynamodb').Table(accounts_table_name)

    def remove_photo(key):
        (uploads / key).unlink(missing_ok=True)
        if bucket:
            s3.delete_object(Bucket=bucket, Key=key)

    @contextmanager
    def connect_local():
        connection = sqlite3.connect(data / 'reports.sqlite3')
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    with connect_local() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                category TEXT NOT NULL, colour TEXT NOT NULL, location TEXT NOT NULL,
                day TEXT NOT NULL, description TEXT NOT NULL, photo TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
                lost_id INTEGER NOT NULL REFERENCES reports(id), found_id INTEGER NOT NULL REFERENCES reports(id),
                proof TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(account_id, lost_id, found_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_approved_claim ON claims(found_id)
                WHERE status IN ('approved', 'returned');
            CREATE UNIQUE INDEX IF NOT EXISTS one_approved_lost ON claims(lost_id)
                WHERE status IN ('approved', 'returned');
            CREATE INDEX IF NOT EXISTS claim_lost_status ON claims(lost_id, status);
            CREATE INDEX IF NOT EXISTS claim_found_status ON claims(found_id, status);
        """)
        db.execute('BEGIN IMMEDIATE')
        columns = {column['name'] for column in db.execute('PRAGMA table_info(reports)')}
        if 'owner_id' not in columns:
            db.execute('ALTER TABLE reports ADD COLUMN owner_id INTEGER REFERENCES accounts(id)')
        if 'withdrawn' not in columns:
            db.execute('ALTER TABLE reports ADD COLUMN withdrawn INTEGER NOT NULL DEFAULT 0')
        if 'display_name' not in {column['name'] for column in db.execute('PRAGMA table_info(accounts)')}:
            db.execute("ALTER TABLE accounts ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE TABLE IF NOT EXISTS app_secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if not os.environ.get('CAMPUS_SECRET_KEY'):
            db.execute("INSERT OR IGNORE INTO app_secrets VALUES ('session', ?)", (token_hex(32),))
            app.secret_key = db.execute("SELECT value FROM app_secrets WHERE name = 'session'").fetchone()['value']
        else:
            app.secret_key = os.environ['CAMPUS_SECRET_KEY']
        db.execute("INSERT OR IGNORE INTO settings VALUES ('sample_reports')")

    reports_table_name = os.environ.get('CAMPUS_RECORDS_TABLE')
    if reports_table_name:
        from dynamo_records import DynamoRecords, RecordsConflict
        records_store = DynamoRecords(reports_table_name)
        records_store.snapshot()

        @app.errorhandler(RecordsConflict)
        def record_write_conflict(_error):
            return render_template('error.html', message='Reports changed. Reopen the page and try again.'), 409

        def connect():
            return records_store.connect(connect_local)
    else:
        connect = connect_local

    def read_reports(query, args=()):
        with connect() as db:
            return db.execute(query, args).fetchall()

    @app.before_request
    def load_session():
        if session.get('admin') and not os.environ.get('CAMPUS_ADMIN_PASSWORD'):
            session.clear()
        if request.endpoint in REPORT_ENDPOINTS and not (
            session.get('account_id') or (session.get('admin') and request.endpoint not in {'report', 'my_reports'})
        ):
            if request.endpoint == 'photo':
                abort(403)
            return redirect(url_for('account', mode='sign-in', next=request.full_path.rstrip('?')))
        g.csrf = session.setdefault('csrf', token_hex(24))
        if request.method == 'POST':
            if not compare_digest(request.form.get('csrf', '').encode(), g.csrf.encode()):
                abort(400)

    @app.after_request
    def prevent_report_caching(response):
        if request.endpoint in REPORT_ENDPOINTS | {'home', 'account', 'admin'}:
            response.headers['Cache-Control'] = 'private, no-store'
        return response

    def can_view(item):
        if item['withdrawn']:
            return bool(session.get('admin'))
        account_id = session.get('account_id')
        return item['report_status'] == 'open' or session.get('admin') or (
            account_id and (item['owner_id'] == account_id or read_reports(
                'SELECT 1 FROM claims WHERE account_id = ? AND (lost_id = ? OR found_id = ?)',
                (account_id, item['id'], item['id']),
            ))
        )

    def get_item(report_id):
        rows = read_reports(REPORTS_WITH_STATUS + ' WHERE id = ?', (report_id,))
        if not rows or not can_view(rows[0]):
            abort(404)
        return rows[0]

    def internal_destination(value):
        if not value.startswith('/') or value.startswith('//') or '\\' in value or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            return ''
        try:
            target = urlsplit(value)
            endpoint, _ = app.url_map.bind_to_environ(request.environ).match(target.path, method='GET')
        except (ValueError, HTTPException):
            return ''
        if target.netloc or target.scheme or target.fragment or endpoint not in REPORT_ENDPOINTS - {'photo'}:
            return ''
        return value

    def create_student(username, password_hash, display_name):
        if accounts_table is not None:
            # Reports still reference the integer account ID in SQLite.
            # Reserve that ID locally before publishing the account.
            with connect_local() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("""
                    INSERT OR IGNORE INTO accounts (username,password_hash,display_name) VALUES (?, '', ?)
                """, (username, display_name))
                identity = db.execute('SELECT id, password_hash FROM accounts WHERE username = ?',
                                      (username,)).fetchone()
                if identity['password_hash']:
                    raise sqlite3.IntegrityError('Existing student account')
            try:
                accounts_table.put_item(
                    Item={'username': username, 'id': identity['id'], 'display_name': display_name,
                          'password_hash': password_hash},
                    ConditionExpression='attribute_not_exists(username)',
                )
            except accounts_table.meta.client.exceptions.ConditionalCheckFailedException as exc:
                raise sqlite3.IntegrityError('Existing student account') from exc
            return
        with connect_local() as db:
            db.execute("""
                INSERT INTO accounts (username,password_hash,display_name) VALUES (?,?,?)
            """, (username, password_hash, display_name))

    def find_student(username):
        if accounts_table is not None:
            if not username or len(username) > 40:
                return None
            student = accounts_table.get_item(Key={'username': username}, ConsistentRead=True).get('Item')
            if student:
                student['id'] = int(student['id'])
            return student
        with connect_local() as db:
            return db.execute('SELECT * FROM accounts WHERE username = ?', (username,)).fetchone()

    @app.route('/account/<any("sign-in",register):mode>', methods=['GET', 'POST'])
    def account(mode):
        error = None
        username = display_name = ''
        destination = internal_destination(request.values.get('next', ''))
        if request.method == 'POST':
            username = request.form.get('username', '').strip().casefold()
            password = request.form.get('password', '')
            if mode == 'register':
                display_name = request.form.get('display_name', '').strip()
                if not 1 <= len(display_name) <= 80:
                    error = 'Enter your name using up to 80 characters.'
                elif not re.fullmatch(r'[a-z0-9_.-]{3,40}', username) or not 8 <= len(password) <= 128:
                    error = 'Use a 3–40 character username and an 8–128 character password.'
                elif password != request.form.get('confirm_password'):
                    error = 'The passwords do not match.'
                else:
                    try:
                        create_student(username, generate_password_hash(password), display_name)
                    except sqlite3.IntegrityError:
                        error = 'That username is unavailable. Choose a different username or sign in.'
            else:
                student = find_student(username)
                if not student or len(password) > 128 or not check_password_hash(student['password_hash'], password):
                    error = 'The username or password is incorrect.'
                else:
                    account_id = student['id']
            if not error:
                session.clear()
                if mode == 'register':
                    session['registration_complete'] = True
                    return redirect(url_for('account', mode='sign-in', next=destination or None), code=303)
                session['account_id'] = account_id
                return redirect(destination or url_for('home'), code=303)
        return render_template('account.html', mode=mode, error=error,
                               username=username, display_name=display_name,
                               destination=destination,
                               registered=mode == 'sign-in' and session.pop('registration_complete', False))

    @app.post('/sign-out')
    def sign_out():
        session.clear()
        return redirect(url_for('home'), code=303)

    @app.get('/my-reports')
    def my_reports():
        if not session.get('account_id'):
            return redirect(url_for('account', mode='sign-in'))
        account_id = session['account_id']
        account = read_reports('SELECT username, display_name FROM accounts WHERE id = ?', (account_id,))[0]
        reports = read_reports(REPORTS_WITH_STATUS + ' WHERE owner_id = ? AND withdrawn = 0 ORDER BY id DESC', (account_id,))
        claims = read_reports("""
            SELECT claims.lost_id, claims.found_id, claims.status,
                CASE WHEN found.withdrawn = 0 THEN found.name ELSE 'Deleted report' END AS name,
                lost.withdrawn OR found.withdrawn AS report_deleted
            FROM claims JOIN reports AS found ON found.id = claims.found_id
            JOIN reports AS lost ON lost.id = claims.lost_id
            WHERE account_id = ? ORDER BY claims.id DESC
        """, (account_id,))
        return render_template('my_reports.html', reports=reports, claims=claims,
                               display_name=account['display_name'] or account['username'],
                               deleted=session.pop('report_deleted', False))

    @app.get('/')
    def home():
        signed_in = session.get('account_id') or session.get('admin')
        reports = read_reports(REPORTS_WITH_STATUS + f' WHERE NOT {LOCKED_REPORT} ORDER BY id DESC LIMIT 3') if signed_in else []
        return render_template('home.html', reports=reports)

    @app.get('/items')
    def browse():
        filters = {key: request.args.get(key, '').strip() for key in ('q', 'kind', 'category', 'location')}
        conditions, args = [f'NOT {LOCKED_REPORT}'], []
        for field in ('kind', 'category', 'location'):
            if filters[field]:
                conditions.append(f'{field} = ?')
                args.append(filters[field])
        if filters['q']:
            conditions.append("instr(lower(name || ' ' || description || ' ' || colour || ' ' || location), lower(?)) > 0")
            args.append(filters['q'])
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        reports = read_reports(REPORTS_WITH_STATUS + where + ' ORDER BY id DESC', args)
        return render_template('browse.html', reports=reports, filters=filters,
                               categories=CATEGORIES, locations=LOCATIONS)

    @app.get('/items/<int:report_id>')
    def detail(report_id):
        item = get_item(report_id)
        matches = []
        if item['report_status'] == 'open':
            for candidate in read_reports(f"""
                SELECT * FROM reports WHERE category = ? AND kind != ? AND day BETWEEN ? AND ?
                AND NOT {LOCKED_REPORT}
            """, (item['category'], item['kind'],
                  (date.fromisoformat(item['day']) - timedelta(days=7)).isoformat(),
                  (date.fromisoformat(item['day']) + timedelta(days=7)).isoformat())):
                lost, found = (item, candidate) if item['kind'] == 'lost' else (candidate, item)
                reasons = match_reasons(lost, found)
                if reasons and (not lost['owner_id'] or lost['owner_id'] != found['owner_id']):
                    matches.append((candidate, reasons, lost['id'], found['id']))
        matches.sort(key=lambda match: (-len(match[1]), -match[0]['id']))
        posted = session.get('posted_report') == report_id
        if posted:
            session.pop('posted_report')
        return render_template('detail.html', item=item, matches=matches, posted=posted)

    @app.post('/items/<int:report_id>/delete')
    def withdraw_report(report_id):
        account_id = session.get('account_id')
        if not account_id or request.form.get('confirm') != 'yes':
            abort(403)
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owner = db.execute('SELECT owner_id FROM reports WHERE id = ?', (report_id,)).fetchone()
            if not owner or owner['owner_id'] != account_id:
                abort(404)
            if not db.execute(f"""
                UPDATE reports SET withdrawn = 1 WHERE id = ? AND NOT {LOCKED_REPORT}
            """, (report_id,)).rowcount:
                abort(409)
            db.execute("""
                UPDATE claims SET status = 'withdrawn'
                WHERE status = 'pending' AND (lost_id = ? OR found_id = ?)
            """, (report_id, report_id))
        session['report_deleted'] = True
        return redirect(url_for('my_reports'), code=303)

    @app.route('/compare/<int:lost_id>/<int:found_id>', methods=['GET', 'POST'])
    def compare(lost_id, found_id):
        lost, found = get_item(lost_id), get_item(found_id)
        if lost['kind'] != 'lost' or found['kind'] != 'found':
            abort(404)
        reasons = match_reasons(lost, found)
        if not reasons:
            abort(404)
        account_id = session.get('account_id')
        claim = None
        if account_id:
            rows = read_reports('SELECT status FROM claims WHERE account_id = ? AND lost_id = ? AND found_id = ?',
                                (account_id, lost_id, found_id))
            claim = rows[0] if rows else None
        available = lost['report_status'] == found['report_status'] == 'open'
        eligible = bool(account_id and lost['owner_id'] == account_id and found['owner_id'] != account_id)
        error = None
        proof = ''
        if request.method == 'POST':
            if not eligible:
                abort(403)
            if not available or claim:
                abort(409)
            proof = request.form.get('proof', '').strip()
            if not 5 <= len(proof) <= 1000:
                error = 'Enter a private ownership detail between 5 and 1,000 characters.'
            else:
                with connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    unavailable = db.execute(f"""
                        SELECT 1 FROM reports WHERE id IN (?, ?) AND {LOCKED_REPORT}
                    """, (lost_id, found_id)).fetchone()
                    if unavailable:
                        abort(409)
                    elif db.execute("""
                        INSERT OR IGNORE INTO claims (account_id,lost_id,found_id,proof) VALUES (?,?,?,?)
                    """, (account_id, lost_id, found_id, proof)).rowcount:
                        return redirect(url_for('my_reports'), code=303)
                    else:
                        abort(409)
        return render_template('compare.html', lost=lost, found=found, reasons=reasons,
                               claim=claim, error=error, proof=proof, available=available,
                               eligible=eligible), 400 if error else 200

    @app.route('/admin', methods=['GET', 'POST'])
    def admin():
        password = os.environ.get('CAMPUS_ADMIN_PASSWORD')
        error = None
        if request.method == 'POST' and password:
            if not compare_digest(request.form.get('password', '').encode(), password.encode()):
                error = 'The admin password is incorrect.'
            else:
                session.clear()
                session['admin'] = True
                return redirect(url_for('admin'), code=303)
        claims = []
        if password and session.get('admin'):
            claims = read_reports("""
                SELECT claims.*, lost.name AS lost_name, found.name AS found_name, accounts.username,
                    EXISTS (SELECT 1 FROM claims AS active WHERE active.status = 'approved'
                        AND (active.lost_id = claims.lost_id OR active.found_id = claims.found_id)
                        AND active.id != claims.id) AS blocked
                FROM claims JOIN reports AS lost ON lost.id = claims.lost_id
                JOIN reports AS found ON found.id = claims.found_id
                JOIN accounts ON accounts.id = claims.account_id
                ORDER BY CASE claims.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                         claims.id DESC
            """)
        return render_template('admin.html', enabled=bool(password), claims=claims, error=error)

    @app.post('/admin/claims/<int:claim_id>/<any(approve,reject,return,cancel):action>')
    def review_claim(claim_id, action):
        if not session.get('admin') or not os.environ.get('CAMPUS_ADMIN_PASSWORD'):
            abort(403)
        previous, status = {'approve': ('pending', 'approved'), 'reject': ('pending', 'rejected'),
                            'return': ('approved', 'returned'), 'cancel': ('approved', 'cancelled')}[action]
        if request.form.get('confirm') != 'yes':
            abort(400)
        try:
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if not db.execute('UPDATE claims SET status = ? WHERE id = ? AND status = ?',
                                  (status, claim_id, previous)).rowcount:
                    abort(409)
                if action == 'return':
                    db.execute("""
                        UPDATE claims SET status = 'superseded' WHERE status = 'pending' AND
                          (lost_id = (SELECT lost_id FROM claims WHERE id = ?) OR
                           found_id = (SELECT found_id FROM claims WHERE id = ?))
                    """, (claim_id, claim_id))
        except sqlite3.IntegrityError:
            abort(409)
        return redirect(url_for('admin'), code=303)

    @app.route('/report/<any(lost,found):kind>', methods=['GET', 'POST'])
    def report(kind):
        if not session.get('account_id'):
            return redirect(url_for('account', mode='sign-in'))
        fields = ('name', 'category', 'colour', 'location', 'day', 'description')
        limits = (100, 40, 40, 40, 10, 1000)
        values = {field: request.form.get(field, '').strip() for field in fields}
        today = date.today()
        errors = []
        if request.method == 'POST':
            if any(not values[field] or len(values[field]) > limit for field, limit in zip(fields, limits)):
                errors.append('Complete each field and keep your description under 1,001 characters.')
            if values['category'] not in CATEGORIES or values['location'] not in LOCATIONS:
                errors.append('Choose a category and campus location from the lists.')
            try:
                reported_day = date.fromisoformat(values['day'])
                if reported_day > today or reported_day.isoformat() != values['day']:
                    raise ValueError
            except ValueError:
                errors.append('Choose today or an earlier date.')
            photo = request.files.get('photo')
            photo_key = None
            if photo and not errors:
                try:
                    photo.stream.seek(0, os.SEEK_END)
                    if photo.stream.tell() > photo_limit:
                        raise ValueError
                    photo.stream.seek(0)
                    with Image.open(photo.stream) as img:
                        if img.format not in {'JPEG', 'PNG', 'WEBP'} or img.width * img.height > 20_000_000:
                            raise ValueError
                        img.verify()
                    photo.stream.seek(0)
                    with Image.open(photo.stream) as img:
                        img.thumbnail((1600, 1600))
                        photo_key = uuid4().hex + '.webp'
                        img.convert('RGB').save(uploads / photo_key, 'WEBP', quality=86)
                except (OSError, ValueError, Image.DecompressionBombError):
                    if photo_key:
                        (uploads / photo_key).unlink(missing_ok=True)
                    errors.append('Choose a valid JPEG, PNG or WebP image up to 6 MB and 20 megapixels.')
            if not errors:
                if bucket and photo_key:
                    try:
                        s3.upload_file(str(uploads / photo_key), bucket, photo_key, ExtraArgs={'ContentType': 'image/webp'})
                    except Exception:
                        app.logger.error('Photo upload to private storage failed.')
                        try:
                            s3.delete_object(Bucket=bucket, Key=photo_key)
                        except Exception:
                            app.logger.error('Could not clean up a failed private photo upload.')
                        return render_template('report.html', kind=kind, values=values,
                                               today=today.isoformat(),
                                               errors=['The photo could not be saved. Select it again to retry.'],
                                               categories=CATEGORIES, locations=LOCATIONS), 503
                    finally:
                        (uploads / photo_key).unlink(missing_ok=True)
                try:
                    with connect() as db:
                        report_id = db.execute("""
                            INSERT INTO reports (kind,name,category,colour,location,day,description,photo,owner_id)
                            VALUES (?,?,?,?,?,?,?,?,?)
                        """, (kind, *(values[field] for field in fields), photo_key, session.get('account_id'))).lastrowid
                except sqlite3.Error:
                    if photo_key:
                        remove_photo(photo_key)
                    raise
                session['posted_report'] = report_id
                return redirect(url_for('detail', report_id=report_id), code=303)
        else:
            values['day'] = today.isoformat()
        return render_template('report.html', kind=kind, values=values, today=today.isoformat(),
                               errors=errors, categories=CATEGORIES, locations=LOCATIONS), 400 if errors else 200

    @app.get('/photos/<filename>')
    def photo(filename):
        if len(filename) != 37 or not filename.endswith('.webp') or not all(
            character in '0123456789abcdef' for character in filename[:32]
        ):
            abort(404)
        rows = read_reports(REPORTS_WITH_STATUS + ' WHERE photo = ?', (filename,))
        if not rows or not can_view(rows[0]):
            abort(404)
        if bucket and not (uploads / filename).exists():
            body = s3.get_object(Bucket=bucket, Key=filename)['Body']
            response = Response(body.iter_chunks(), mimetype='image/webp')
            response.call_on_close(body.close)
            return response
        return send_from_directory(uploads, filename, mimetype='image/webp')

    @app.errorhandler(413)
    def too_large(_error):
        return render_template('error.html', message='That upload is too large. Choose a photo smaller than 6 MB.'), 413

    @app.errorhandler(404)
    def missing(_error):
        return render_template('error.html', message='That page or report could not be found.'), 404

    @app.errorhandler(400)
    def invalid_request(_error):
        return render_template('error.html', message='Reopen the form and try again.'), 400

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template('error.html', message='Your account cannot perform this action.'), 403

    @app.errorhandler(409)
    def conflict(_error):
        if request.endpoint == 'withdraw_report':
            return render_template('error.html', message='Only open reports can be deleted.',
                                   recovery=url_for('my_reports'), recovery_label='My reports'), 409
        destination = 'admin' if session.get('admin') else 'my_reports'
        return render_template('error.html', message='This claim is already submitted or its availability has changed.',
                               recovery=url_for(destination), recovery_label='Review claims' if destination == 'admin' else 'My reports'), 409

    return app


if __name__ == '__main__':
    create_app().run()
