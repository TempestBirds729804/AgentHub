# AgentHub - Docker Compose Deployment

MinIO is built locally from official source commit `7aac2a2c5b7c882e68c1ce017d8256be2feea27f` using `docker/minio/Dockerfile`; no prebuilt MinIO or mc image is required. The initial build needs GitHub, Go modules and official Go/Debian base images. The image includes the AGPL license and source revision label; [corresponding source](https://github.com/minio/minio/tree/7aac2a2c5b7c882e68c1ce017d8256be2feea27f) is pinned. Review upstream security updates before changing the pin. Volume names and S3 configuration are unchanged; back up existing data before upgrading an existing deployment.

You can deploy the project to your own remote server with Docker Compose. The deployment configuration includes Traefik to handle HTTPS and route incoming traffic to the application.

This is the full-platform deployment path: PostgreSQL/pgvector, Redis, MinIO, API/frontend, arq worker and the read-only MCP service. The FastAPI Cloud workflow deploys only the API and is opt-in.

## Preparation

* Have a remote server ready and available.
* Configure DNS records pointing to the server for the application domain and any supporting service subdomains you want to expose, such as `fastapi-project.example.com` and `adminer.fastapi-project.example.com`.
* Install and configure [Docker](https://docs.docker.com/engine/install/) on the remote server (Docker Engine, not Docker Desktop).

## Copy the Code

```bash
rsync -av --exclude=".git/" --filter=":- .gitignore" ./ root@your-server.example.com:/root/code/app/
```

The `--filter=":- .gitignore"` option tells `rsync` to use the same ignore rules as Git, excluding files such as the Python virtual environment.

## Configure the Application

### Environment Variables

Set the application domain, project name, and first superuser email:

```bash
export DOMAIN=fastapi-project.example.com
export PROJECT_NAME="AgentHub"
export FIRST_SUPERUSER=admin@example.com
export COMPOSE_PROJECT_NAME=agenthub
export COMPOSE_ENV_FILES=/dev/null
```

You can also configure these environment variables as needed:

* `SMTP_HOST`: The SMTP server host from your email provider.
* `SMTP_USER`: The SMTP server user.
* `EMAILS_FROM_EMAIL`: The email account used to send emails.
* `SENTRY_DSN`: The DSN for Sentry.

### Secrets

Generate and set secure values for the database password, token signing key, and first superuser password:

```bash
export POSTGRES_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export FIRST_SUPERUSER_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

To use an authenticated email provider, also set `SMTP_PASSWORD`.

Also set `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `LLM_API_KEY` and `EMBEDDING_API_KEY` from your secret store. Do not use `changethis` or `minioadmin` in production. Configure `LLM_BASE_URL`/`LLM_MODEL` for your OpenAI-compatible provider; configure `EMBEDDING_BASE_URL`/`EMBEDDING_MODEL` for DashScope, retaining the migrated 1536 dimensions. `S3_BUCKET` defaults to `agenthub`.

The ignored root `.env` is for local development only. `COMPOSE_ENV_FILES=/dev/null` prevents accidentally loading it on the deployment host. Environment variables must be present for every Compose command. Preserve the existing Compose project name and database/S3 credentials when updating an existing deployment: changing environment passwords does not migrate stored database users or storage credentials. Never run `down -v` on production data.

## Deploy

```bash
cd /root/code/app/
docker compose -f compose.yml -f compose.deploy.yml build
docker compose -f compose.yml -f compose.deploy.yml run --rm backend bash scripts/prestart.sh
docker compose -f compose.yml -f compose.deploy.yml up -d --wait --wait-timeout 180
docker compose -f compose.yml -f compose.deploy.yml exec -T backend alembic check
docker compose -f compose.yml -f compose.deploy.yml exec -T backend curl --fail http://localhost:8000/
```

The `compose.deploy.yml` file adds HTTPS and automatic certificate handling to the shared `compose.yml` configuration. Explicitly listing both files excludes the local settings from `compose.override.yml`.

The backend Docker image builds the frontend, so the server does not need Bun or prebuilt frontend files.

## Deploy with GitHub Actions

The included `.github/workflows/deploy-docker-compose.yml` workflow runs the deployment commands on the server when manually triggered from GitHub Actions.

Use a self-hosted runner only for a repository whose contributors and workflow code you trust. GitHub recommends using self-hosted runners with private repositories because workflows execute directly on the runner machine.

### Configure Repository Variables and Secrets

In the repository, go to **Settings** > **Secrets and variables** > **Actions** and add these repository variables:

* `DOMAIN`
* `PROJECT_NAME`
* `FIRST_SUPERUSER`

Optional variables: `COMPOSE_PROJECT_NAME` (keep stable; defaults to `agenthub`), `LLM_BASE_URL`, `LLM_MODEL`, `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, `S3_BUCKET`.

To enable emails, add these optional repository variables:

* `SMTP_HOST`
* `SMTP_USER`
* `EMAILS_FROM_EMAIL`

To enable Sentry, add the optional `SENTRY_DSN` repository variable.

Add these repository secrets:

* `POSTGRES_PASSWORD`
* `SECRET_KEY`
* `FIRST_SUPERUSER_PASSWORD`
* `S3_ACCESS_KEY`
* `S3_SECRET_KEY`
* `LLM_API_KEY`
* `EMBEDDING_API_KEY`

Configure the `production` GitHub environment and its runner access/review rules. The workflow validates required values before building, applies migrations and storage/checkpoint setup, waits for all service health checks, and verifies schema and frontend. Repository or environment secrets are injected without printing their values. No workflow performs an automatic production volume reset.

To use an authenticated email provider, add the optional `SMTP_PASSWORD` repository secret.

### Install a Self-Hosted Runner

On the server, create a dedicated user and grant it access to Docker:

```bash
sudo adduser github
sudo usermod -aG docker github
sudo su - github
```

In the GitHub repository, go to **Settings** > **Actions** > **Runners**, select **New self-hosted runner**, choose Linux, and follow the commands GitHub provides to download, configure, and register the runner. Install it in `/home/github/actions-runner`.

After registering the runner, exit the `github` user session and install the runner as a system service:

```bash
exit
cd /home/github/actions-runner
sudo ./svc.sh install github
sudo ./svc.sh start
sudo ./svc.sh status
```

See GitHub's guides for [adding a self-hosted runner](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners) and [configuring the runner as a service](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application?platform=linux).

### Run the Deployment

When the runner is online, open the repository's **Actions** tab, select **Deploy with Docker Compose**, and select **Run workflow**.

## URLs

Replace `fastapi-project.example.com` with your domain.

Application (frontend and API): `https://fastapi-project.example.com`

Interactive API docs: `https://fastapi-project.example.com/docs`

Adminer: `https://adminer.fastapi-project.example.com`
