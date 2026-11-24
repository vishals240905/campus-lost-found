# Campus Lost & Found

A local Flask website for reporting lost and found items on campus. SQLite keeps reports after a refresh or restart. Optional photos are stored as WebP files on disk.

## Run on Windows

In PowerShell, from this folder, run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Open <http://127.0.0.1:5000>. If you already have Flask and Pillow in Anaconda on this PC, you can run the app directly:

```powershell
& "$env:USERPROFILE\anaconda3\python.exe" app.py
```

## Demonstrate the first milestone

1. Create a student account or sign in. Click **Report lost item** and enter **Black wallet**.
2. Choose **Accessories** and **Central Library**, enter the colour and a description, then submit. A photo is optional. Submission opens the new report.
3. Confirm the report appears on Browse Items. Apply the same category and location filters to find it again.
4. Use **Report found item** to demonstrate the other report form.

## Project files

- `app.py` handles pages, validates reports and photos, and reads and writes SQLite.
- `templates/` contains the shared navigation, homepage, report forms, listings, and detail pages.
- `static/style.css` styles the site for desktop and mobile.
- `instance/` is created at startup and holds the local database and uploaded photos.
- `requirements.txt` lists the Python dependencies.

New local databases start empty. Set `CAMPUS_DATA_DIR` to a different directory to use a separate set of reports and photos.

## Demonstrate matching and claims

1. Register and sign in with two student accounts. Use one for the lost **Black Wallet** and the other for the found **Black Wallet** at **Central Library** in **Accessories**. Use the same date or a found date within seven days after the loss. Submit an unrelated report in a different category.
2. As the lost-report owner, open the suggested match and submit a private ownership detail. **My reports** shows **Pending review**. Both wallet reports remain open until approval.
3. Open [Admin review](http://127.0.0.1:5001/admin) in a separate browser profile. Review the proof and confirm **Approve claim**. Both reports move to **Awaiting handover** and disappear from open Browse and matching. **Cancel approval** reopens both if the handover falls through.
4. After handing over the item, confirm **Mark as returned**. Check **My reports** for both students and **Claim history** for the admin. Verify that the unrelated report is still in Browse, then restart Flask and verify the returned status again.

To enable admin review, stop the running server and start it from the VS Code PowerShell terminal using these commands. The prompt masks the password while you enter it.

```powershell
$env:CAMPUS_ADMIN_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host 'Choose the local admin password' -AsSecureString)).Password
& "$env:USERPROFILE\anaconda3\python.exe" -m flask --app app run --port 5001
```

To keep demo data separate, set `$env:CAMPUS_DATA_DIR = Join-Path $PWD 'instance\demo'` before starting Flask. Restart using the same directory to verify persistence.

Existing reports and claims are retained. Reports created before accounts existed can still appear in open Browse and matching, but an ownership claim requires a lost report tied to the signed-in owner. Closed reports and their photos are available to the relevant owners, claimants, and admin.

Run the workflow regression test with `python -m unittest -v test_workflow`.

See [AWS deployment and assignment notes](AWS.md) for the state of the cloud work and the assignment deliverables.
