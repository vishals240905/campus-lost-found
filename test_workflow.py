import os
import re
import sqlite3
import unittest
from contextlib import closing
from copy import deepcopy
from threading import Lock
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from app import create_app, match_reasons
from dynamo_records import DynamoRecords, RecordsConflict, rows_by_key


class ClaimWorkflowTest(unittest.TestCase):
    def test_existing_report_can_be_matched_claimed_and_returned(self):
        self.check_claim_workflow()

    def test_dynamodb_records_workflow(self):
        class MemoryCloud(DynamoRecords):
            def __init__(self, records):
                self.records = records
                self.revision = 1
                self.lock = Lock()

            def snapshot(self):
                with self.lock:
                    return self.revision, deepcopy(self.records)

            def commit(self, version, previous, records):
                with self.lock:
                    if version != self.revision:
                        raise RecordsConflict('Another write won')
                    if previous != records:
                        self.records = deepcopy(records)
                        self.revision += 1

        self.check_claim_workflow(MemoryCloud)

    def check_claim_workflow(self, backend=None):
        today = date.today().isoformat()
        with TemporaryDirectory(dir='.') as folder, patch.dict(os.environ, {
            'CAMPUS_ADMIN_PASSWORD': 'test-only-password', 'CAMPUS_PHOTO_BUCKET': '',
            'CAMPUS_ACCOUNTS_TABLE': '', 'CAMPUS_RECORDS_TABLE': '',
        }):
            with closing(sqlite3.connect(Path(folder) / 'reports.sqlite3')) as db, db:
                db.executescript("""
                    CREATE TABLE reports (
                        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                        category TEXT NOT NULL, colour TEXT NOT NULL, location TEXT NOT NULL,
                        day TEXT NOT NULL, description TEXT NOT NULL, photo TEXT
                    );
                    CREATE TABLE settings (name TEXT PRIMARY KEY);
                    INSERT INTO settings VALUES ('sample_reports');
                """)
                db.execute("""
                    INSERT INTO reports VALUES (
                        1, 'lost', 'Black wallet', 'Accessories', 'Black', 'Central Library',
                        ?, 'Wallet near study desks', NULL
                    )
                """, (today,))

            create_app(folder)
            if backend:
                with closing(sqlite3.connect(Path(folder) / 'reports.sqlite3')) as source:
                    source.row_factory = sqlite3.Row
                    store = backend(rows_by_key(source))
                env = patch.dict(os.environ, {'CAMPUS_RECORDS_TABLE': 'test-records'})
                mock = patch('dynamo_records.DynamoRecords', return_value=store)
                env.start()
                mock.start()
                self.addCleanup(env.stop)
                self.addCleanup(mock.stop)
            app = create_app(folder)
            app.testing = True
            student, admin, guest, finder, rival = (app.test_client() for _ in range(5))

            def submit(client, path, fields, form_path=None):
                form = client.get(form_path or path)
                csrf = re.search(rb'name="csrf" value="([^"]+)', form.data).group(1).decode()
                return client.post(path, data={**fields, 'csrf': csrf})

            self.assertNotIn(b'Black wallet', guest.get('/').data)
            for path in ('/items', '/items/1', '/report/lost', '/compare/1/2'):
                self.assertIn('/account/sign-in?next=', guest.get(path).location)
            self.assertNotIn(b'Confirm password', guest.get('/account/sign-in').data)
            self.assertIn(b'Confirm password', guest.get('/account/register').data)
            registration = {
                'display_name': 'Test Student', 'username': 'student',
                'password': 'student-password', 'confirm_password': 'student-password',
            }
            self.assertIn(b'passwords do not match', submit(student, '/account/register', {
                **registration, 'confirm_password': 'different-password',
            }).data)
            registered = submit(student, '/account/register', registration)
            self.assertEqual(registered.status_code, 303)
            self.assertTrue(registered.location.endswith('/account/sign-in'))
            self.assertEqual(student.get('/items').status_code, 302)
            self.assertIn(b'Your account was created', student.get(registered.location).data)
            self.assertEqual(submit(student, '/account/sign-in', {
                'username': 'student', 'password': 'student-password', 'next': '//example.com',
            }).location, '/')
            for client, username in ((finder, 'finder'), (rival, 'rival')):
                fields = {**registration, 'username': username}
                self.assertEqual(submit(client, '/account/register', fields).status_code, 303)
                self.assertEqual(submit(client, '/account/sign-in', {
                    'username': username, 'password': registration['password'], 'next': '/report/lost',
                }).location, '/report/lost')
            with closing(sqlite3.connect(Path(folder) / 'reports.sqlite3')) as db:
                password_hash = db.execute("SELECT password_hash FROM accounts WHERE username = 'student'").fetchone()[0]
                self.assertNotEqual(password_hash, registration['password'])
            image = BytesIO()
            Image.new('RGB', (20, 20)).save(image, 'PNG')
            image.seek(0)
            wallet = {
                'name': 'Black wallet', 'category': 'Accessories', 'colour': 'black',
                'location': 'Central Library', 'day': today, 'description': 'Wallet by the study desks',
            }
            self.assertEqual(submit(finder, '/report/found', {
                **wallet, 'name': 'Found black wallet', 'photo': (image, 'wallet.png'),
            }).location, '/items/2')
            self.assertEqual(submit(student, '/report/lost', wallet).location, '/items/3')
            self.assertEqual(submit(rival, '/report/lost', wallet).location, '/items/4')
            self.assertEqual(submit(finder, '/report/lost', wallet).location, '/items/5')
            self.assertEqual(submit(finder, '/report/found', {
                **wallet, 'name': 'Unrelated calculator', 'category': 'Electronics',
            }).location, '/items/6')
            invalid_photo = submit(finder, '/report/found', {**wallet, 'photo': (BytesIO(b'not a photo'), 'bad.png')})
            self.assertEqual(invalid_photo.status_code, 400)
            self.assertEqual(len(list((Path(folder) / 'uploads').iterdir())), 1)
            if not backend:
                with closing(sqlite3.connect(Path(folder) / 'reports.sqlite3')) as db, db:
                    db.execute("""
                        CREATE TRIGGER fail_demo_insert BEFORE INSERT ON reports
                        WHEN NEW.name = 'Simulated failed write'
                        BEGIN SELECT RAISE(ABORT, 'Simulated failure'); END
                    """)
            rejected_image = BytesIO()
            Image.new('RGB', (20, 20)).save(rejected_image, 'PNG')
            rejected_image.seek(0)
            if backend:
                with patch.object(store, 'commit', side_effect=RecordsConflict('Simulated conflict')):
                    self.assertEqual(submit(finder, '/report/found', {
                        **wallet, 'name': 'Simulated failed write', 'photo': (rejected_image, 'valid.png'),
                    }).status_code, 409)
            else:
                with self.assertRaises(sqlite3.IntegrityError):
                    submit(finder, '/report/found', {
                        **wallet, 'name': 'Simulated failed write', 'photo': (rejected_image, 'valid.png'),
                    })
            self.assertEqual(len(list((Path(folder) / 'uploads').iterdir())), 1)
            self.assertIn(b'Found black wallet', student.get('/items/1').data)
            self.assertIn(b'Found black wallet', finder.get('/my-reports').data)

            comparison = '/compare/3/2'
            compared = student.get(comparison).data
            self.assertIn(b'Same colour', compared)
            photo_url = re.search(rb'/photos/[a-f0-9]+\.webp', compared).group().decode()
            self.assertEqual(guest.get(photo_url).status_code, 403)
            with student.get(photo_url) as photo:
                self.assertEqual(photo.mimetype, 'image/webp')
                self.assertEqual(photo.headers['Cache-Control'], 'private, no-store')
            proof = b'Blue sticker in coin pocket'
            self.assertEqual(student.post(comparison, data={'proof': proof.decode()}).status_code, 400)
            self.assertEqual(submit(finder, '/compare/5/2', {'proof': proof.decode()}).status_code, 403)
            self.assertEqual(submit(rival, comparison, {'proof': proof.decode()}).status_code, 403)
            self.assertEqual(submit(student, '/compare/1/2', {'proof': proof.decode()}).status_code, 403)
            self.assertEqual(submit(student, comparison, {'proof': proof.decode()}).status_code, 303)
            self.assertEqual(submit(student, comparison, {'proof': proof.decode()}).status_code, 409)
            self.assertEqual(submit(rival, '/compare/4/2', {'proof': 'My name is inside.'}).status_code, 303)
            self.assertEqual(submit(student, '/report/lost', wallet).location, '/items/7')
            self.assertEqual(submit(student, '/compare/7/2', {'proof': 'A competing detail.'}).status_code, 303)
            self.assertIn(b'Pending', student.get('/my-reports').data)
            self.assertIn(b'Found black wallet', student.get('/items').data)
            self.assertNotIn(proof, guest.get(comparison).data)
            self.assertNotIn(proof, guest.get('/admin').data)
            approval = '/admin/claims/1/approve'
            self.assertEqual(submit(guest, approval, {}, '/admin').status_code, 403)
            self.assertEqual(submit(admin, '/admin', {'password': 'test-only-password'}).status_code, 303)
            self.assertIn(proof, admin.get('/admin').data)
            self.assertEqual(submit(admin, approval, {}, '/admin').status_code, 400)
            confirmation = {'confirm': 'yes'}
            admin_two = app.test_client()
            self.assertEqual(submit(admin_two, '/admin', {'password': 'test-only-password'}).status_code, 303)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [
                    pool.submit(submit, client, f'/admin/claims/{claim_id}/approve', confirmation, '/admin')
                    for client, claim_id in ((admin, 1), (admin_two, 2))
                ]
                codes = [result.result().status_code for result in results]
            self.assertEqual(sorted(codes), [303, 409])
            winner = codes.index(303) + 1
            loser = 3 - winner
            self.assertNotIn(b'Found black wallet', student.get('/items').data)
            self.assertIn(b'Awaiting handover', finder.get('/items/2').data)
            self.assertNotIn(b'Possible matches', finder.get('/items/2').data)
            self.assertEqual(submit(admin, f'/admin/claims/{winner}/cancel', confirmation, '/admin').status_code, 303)
            self.assertIn(b'Found black wallet', student.get('/items').data)
            self.assertEqual(submit(admin, f'/admin/claims/{loser}/approve', confirmation, '/admin').status_code, 303)
            third = app.test_client()
            self.assertEqual(submit(third, '/account/register', {**registration, 'username': 'third'}).status_code, 303)
            self.assertEqual(submit(third, '/account/sign-in', {
                'username': 'third', 'password': registration['password'],
            }).status_code, 303)
            self.assertEqual(submit(third, '/report/lost', wallet).location, '/items/8')
            self.assertEqual(third.get('/items/2').status_code, 404)
            self.assertEqual(third.get(photo_url).status_code, 404)
            self.assertNotIn(b'Found black wallet', third.get('/items/8').data)
            self.assertEqual(submit(admin, f'/admin/claims/{loser}/return', confirmation, '/admin').status_code, 303)
            self.assertIn(b'Returned', finder.get('/my-reports').data)
            self.assertIn(b'Claim history', admin.get('/admin').data)
            self.assertIn(b'Closed after another return', student.get('/my-reports').data)
            self.assertEqual(third.get('/items/2').status_code, 404)
            restarted = create_app(folder).test_client()
            self.assertEqual(submit(restarted, '/account/sign-in', {
                'username': 'student', 'password': 'student-password',
            }).status_code, 303)
            self.assertNotIn(b'Found black wallet', restarted.get('/items').data)
            self.assertNotIn(b'Found black wallet', restarted.get('/').data)
            self.assertIn(b'Unrelated calculator', restarted.get('/items').data)
            self.assertEqual(submit(restarted, '/sign-out', {}, '/items').status_code, 303)
            self.assertEqual(restarted.get(photo_url).status_code, 403)
            self.assertIn(b'Black wallet', student.get('/items/1').data)
            self.assertEqual(submit(admin, approval, confirmation, '/admin').status_code, 409)
            self.assertEqual(submit(finder, '/items/2/delete', confirmation, '/items/2').status_code, 409)
            self.assertNotIn(b'Confirm delete report', student.get('/items/6').data)
            self.assertEqual(submit(student, '/items/6/delete', confirmation, '/items/6').status_code, 404)
            self.assertEqual(finder.post('/items/6/delete', data=confirmation).status_code, 400)
            self.assertEqual(submit(finder, '/items/6/delete', confirmation, '/items/6').status_code, 303)
            self.assertIn(b'Your report was deleted', finder.get('/my-reports').data)
            self.assertNotIn(b'Unrelated calculator', student.get('/items').data)
            self.assertNotIn(b'Unrelated calculator', finder.get('/').data)
            self.assertEqual(finder.get('/items/6').status_code, 404)

    def test_match_dates(self):
        today = date.today()
        item = dict(category='Accessories', colour='Black', location='Central Library',
                    day=today.isoformat(), name='Wallet', description='Leather wallet')
        self.assertIn('Lost and found on the same day', match_reasons(item, item))
        self.assertIn('Found one day after it was lost', match_reasons(item, {
            **item, 'day': (today + timedelta(days=1)).isoformat(),
        }))
        for gap in (-1, 8):
            self.assertEqual(match_reasons(item, {**item, 'day': (today + timedelta(days=gap)).isoformat()}), [])
