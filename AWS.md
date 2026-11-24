# Campus Lost & Found on AWS Learner Lab

## Deployment status

The Learner Lab demo in N. Virginia (`us-east-1`) runs Gunicorn on the `campus-lost-found` EC2 instance and uses the private `campus-lost-found-photos-2409` S3 bucket. Both DynamoDB tables are enabled. The live demo verified account registration and sign-in, loading a migrated S3 photo, new lost and found reports, and a watch claim progressing from pending review to returned. The returned claim appeared in student history and both reports disappeared from active Browse. SQLite remains on EC2 for account references and app metadata.

## Resume the existing demo

1. Start the Learner Lab and check its spend against the $30 project budget. In EC2, start the existing instance if stopped and check its current Public IPv4 address. The address can change after a stop/start.
2. From VS Code PowerShell, connect to the existing deployment. Replace the IP placeholder with the instance's current value. Leave the tunnel open while you use the website.

   ```powershell
   $key = "$env:USERPROFILE\Downloads\campus-lost-found-key.pem"
   $ec2Ip = "CURRENT_EC2_PUBLIC_IPV4"
   ssh -o ExitOnForwardFailure=yes -o IdentitiesOnly=yes -i $key -L 8000:127.0.0.1:8000 "ec2-user@$ec2Ip"
   ```

3. In the connected EC2 shell, use the existing project directory and virtual environment. The password prompt hides the password while you type.

   ```bash
   cd ~/campus-lost-found
   export CAMPUS_PHOTO_BUCKET='campus-lost-found-photos-2409'
   export AWS_DEFAULT_REGION='us-east-1'
   export CAMPUS_ACCOUNTS_TABLE='campus-lost-found-accounts'
   export CAMPUS_RECORDS_TABLE='campus-lost-found'
   read -rsp 'Choose the EC2 demo admin password: ' CAMPUS_ADMIN_PASSWORD
   echo
   export CAMPUS_ADMIN_PASSWORD
   .venv/bin/gunicorn --bind 127.0.0.1:8000 'app:create_app()'
   ```

4. Open [the private demo](http://127.0.0.1:8000) and check that an existing test account can sign in, a report photo loads, and a returned claim still appears in history. In the console, record the instance's **Monitoring** graphs and the bucket's **Objects** list. Stop the instance once the demo is finished if you are saving it for another session.

## EC2 and S3 deployment

1. In the Learner Lab's permitted region, create a private S3 bucket with Block Public Access enabled. Launch an allowed Linux EC2 instance with an instance profile that can put, get, and delete objects in that bucket. Use the lab-provided instance profile if it has those permissions.
2. Copy the application source to EC2. Use a fresh data directory for the demo so personal test uploads and local credentials stay on the development PC.
3. On Linux, run `python3 -m venv .venv`, then `.venv/bin/pip install -r requirements-aws.txt`. Configure `CAMPUS_DATA_DIR` on persistent storage, `CAMPUS_PHOTO_BUCKET`, `AWS_DEFAULT_REGION`, `CAMPUS_SECRET_KEY` and `CAMPUS_ADMIN_PASSWORD` in a protected service environment file. Use a randomly generated secret key and a unique admin password. Credentials come from the EC2 role.
4. Run `.venv/bin/gunicorn --bind 127.0.0.1:8000 'app:create_app()'` under a dedicated service user with write access to the data directory. Configure a TLS reverse proxy for external access and set `CAMPUS_SECURE_COOKIES=1` for HTTPS. For a local HTTP demo through an SSH tunnel, leave that setting unset. Restrict incoming connections to the required port and limit SSH to the operator's IP.
5. Verify the assignment demo with two wallet reports. Open the image, confirm its object exists in S3, approve and return the item, and verify the records after an app restart. Capture the hosted URL and service screenshots for submission.

[EC2 instance profiles](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html)

## DynamoDB design

The optional lab storage configuration uses `CAMPUS_ACCOUNTS_TABLE=campus-lost-found-accounts` for registration and password checks and `CAMPUS_RECORDS_TABLE=campus-lost-found` for report and claim records. The EC2 SQLite file still supplies integer account references and app metadata. Keep it on the EC2 disk. DynamoDB is the authoritative store for reports and claims in this mode.

After backing up SQLite and stopping all app writers, run the migration from the EC2 project directory with the EC2 role and N. Virginia region:

```bash
AWS_DEFAULT_REGION=us-east-1 .venv/bin/python dynamo_records.py instance/reports.sqlite3 campus-lost-found
```

The migration compares all written records with SQLite and refuses to overwrite conflicting DynamoDB records. Once it succeeds, export both table variables before starting Gunicorn. Keep the existing S3 and admin environment settings.

The lab implementation loads reports and claims into a transient SQLite view for filtering, matching, and joins. It writes back only changed DynamoDB items, with a revision check and one transaction for each approval or return. This is intended for a small lab dataset. Initial migration and any single operation must fit within 99 changed records plus the revision record. The EC2 role needs `GetItem`, `Query`, and transactional write permissions for the records table.

Use on-demand capacity. Reports and claims are distinct items identified by entity type and ID. The S3 object key is stored alongside its report. Private claim proof is restricted by the application.

Use a conditional write when creating claims and a DynamoDB transaction when reserving a lost and found record during approval. Require the claim to be pending and both reports to be available. This preserves the current rule that one active approved claim may reserve each item. Verify that simultaneous approvals cannot reserve the same report twice. [DynamoDB transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html)

## Monitoring and CI/CD design

- The EC2 SDK session uses temporary instance-role credentials for the Learner Lab's `LabRole`. This role was verified on EC2. The app uses it for S3 and DynamoDB, and the role was left unchanged.
- CloudWatch alarm `campus-lost-found-ec2-status-i-0233536dcd209c9a7` watches `AWS/EC2` `StatusCheckFailed` for this instance in `us-east-1`. Two consecutive one-minute failures trigger the alarm. It has no notification or EC2 actions. The alarm and the underlying metric samples were verified on AWS. [EC2 status alarms](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/creating_status_check_alarms.html)
- CloudWatch alarm `campus-lost-found-dynamodb-write-throttles` monitors `WriteThrottleEvents` on the reports and claims table. Alarm `campus-lost-found-s3-server-errors` monitors `5xxErrors` on the photo bucket using its `campus-photo-requests` request metrics configuration. Each checks for at least one event in five minutes, treats missing data as non-breaching, and has no notification or automated actions. [DynamoDB metrics](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/metrics-dimensions.html) · [S3 request metrics](https://docs.aws.amazon.com/AmazonS3/latest/userguide/configure-request-metrics-bucket.html)
- Show EC2 status checks and CPU utilization in CloudWatch. If the lab role permits it, send Gunicorn access and error logs through the CloudWatch agent with a seven-day retention policy. Capture metrics during the demo for the assignment. [CloudWatch agent prerequisites](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/prerequisites.html)
- The GitHub Actions **CI** workflow installs dependencies and runs the workflow tests on pushes and pull requests. **Deploy to Learner Lab EC2** runs manually from `main`, reruns CI, and sends the tested checkout to a trusted Linux runner on the existing EC2 instance. The deployment updates application source, reloads Gunicorn and verifies the homepage. It can restore the previous source if the reload fails. SQLite, S3 and DynamoDB are outside the source replacement.
- Keep private proof and authentication fields out of logs and listing pages. Keep secrets outside Git. Restrict the S3 bucket and database permissions to the EC2 role and the required resources.

### Connect GitHub Actions once

1. Publish the source to a private GitHub repository with `main` as its default branch. Keep the repository available only to trusted collaborators. The deployment runner has access to the EC2 instance and its Learner Lab role.
2. In GitHub, open **Settings → Actions → Runners → New self-hosted runner**. Follow GitHub's Linux x64 instructions on EC2 as `ec2-user`. Install it in a directory separate from the project. During configuration, add the custom label `campus-learner-lab`. Keep the runner registration token out of the repository. [GitHub runner setup](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
3. Start the runner in its own EC2 shell with `./run.sh`. When starting Gunicorn manually for deployment, use the normal environment settings and add a PID file:

   ```bash
   .venv/bin/gunicorn --pid gunicorn.pid --bind 127.0.0.1:8000 'app:create_app()'
   ```

4. In GitHub, open **Actions → Deploy to Learner Lab EC2 → Run workflow** on `main`. Confirm both jobs pass and open the website through the SSH tunnel. Start the runner and Gunicorn again after a lab shutdown. If dependency files change, update the EC2 virtual environment and deployed requirement files before triggering CD.

## Estimated cost and proposed SLA

For a 730-hour Linux demo month, a t3.micro at $0.0104/hour, one public IPv4 address at $0.005/hour and an 8 GB gp3 disk at $0.08/GB-month total about **$11.88 before storage requests, data transfer, logs, taxes and any CPU surplus charges**. This leaves **$18.12** within the $30 project budget for actual usage. Update the estimate using the lab's region and expected running hours. Check the lab credit balance before and after demos. [EC2 T3 pricing](https://aws.amazon.com/ec2/instance-types/t3/) · [EBS pricing](https://aws.amazon.com/ebs/pricing/) · [Public IPv4 pricing](https://aws.amazon.com/vpc/pricing/)

The proposed student-project SLA is 99% monthly availability during published Learner Lab demo hours, measured with periodic HTTPS checks. Restore service within four lab hours and take daily backups during active use, with a target of at most 24 hours of data loss. A single-instance lab setup has no automatic failover and lab session suspensions are outside the published window.
