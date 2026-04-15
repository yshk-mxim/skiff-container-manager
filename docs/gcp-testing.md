# Using SKIFF Container Manager on GCP

A practical guide to day-to-day operations once the app is running on a Cloud Workstation.
For initial setup and environment variables see [deployment.md](deployment.md).

## Connecting

```bash
# Open the tunnel first (once per session)
ssh -fNL /tmp/docker.sock:/var/run/docker.sock dev@<DOCKER_VM>

TOKEN="<your API_TOKEN>"
BASE="http://127.0.0.1:8080"   # port set by run.sh
```

All mutating requests (POST, DELETE) require an extra CSRF header:

```bash
curl -H "Authorization: Bearer $TOKEN" ...                            # read
curl -H "Authorization: Bearer $TOKEN" \
     -H "X-Requested-With: ContainerManager" -X POST ...             # write
```

---

## Health check

```bash
curl $BASE/health          # always 200 if the app is up
curl -H "Authorization: Bearer $TOKEN" $BASE/ready
# 200 = Docker VM reachable via SSH
# 503 = SSH tunnel broken — fix DOCKER_HOST or the VM before proceeding
```

---

## Images

### Pull from Artifact Registry

```bash
IMAGE="us-docker.pkg.dev/<PROJECT>/<REPO>/<image>:<tag>"

curl -sf -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Requested-With: ContainerManager" \
  "$BASE/api/images/pull?image=$IMAGE"
```

### List images

```bash
curl -sf -H "Authorization: Bearer $TOKEN" $BASE/api/images | python3 -m json.tool

# Only images from allowed registries:
curl -sf -H "Authorization: Bearer $TOKEN" $BASE/api/images/allowed | python3 -m json.tool
```

### Inspect / delete

```bash
curl -sf -H "Authorization: Bearer $TOKEN" $BASE/api/images/<short-id>/inspect

curl -sf -X DELETE \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Requested-With: ContainerManager" \
  $BASE/api/images/<short-id>
```

---

## Containers

### Run

```bash
curl -sf -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Requested-With: ContainerManager" \
  -H "Content-Type: application/json" \
  -d '{"environment": ["PORT=8080"], "labels": {"app": "myapp"}}' \
  "$BASE/api/containers/run?image=$IMAGE"
```

### List / inspect

```bash
curl -sf -H "Authorization: Bearer $TOKEN" $BASE/api/containers
curl -sf -H "Authorization: Bearer $TOKEN" $BASE/api/containers/<id>/inspect
```

### Lifecycle

```bash
ID="<container-id>"
for ACTION in start stop restart kill; do
  curl -sf -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Requested-With: ContainerManager" \
    "$BASE/api/containers/$ID/$ACTION"
done

# Delete
curl -sf -X DELETE \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Requested-With: ContainerManager" \
  $BASE/api/containers/$ID
```

### Logs

```bash
# Last 100 lines
curl -sf -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/containers/$ID/logs?tail=100"
```

### Interactive exec (WebSocket)

WebSocket auth is message-based (HTTP headers are not used):

```bash
# wscat must be installed: npm install -g wscat
wscat -c "ws://127.0.0.1:8080/ws/exec/$ID"
```

Immediately after the connection opens, send the auth frame as a plain-text message:

```
AUTH <API_TOKEN>
```

After the server acknowledges auth, stdin/stdout flow as plain text frames.

---

## Compose

```bash
# Upload and start a compose file
curl -sf -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Requested-With: ContainerManager" \
  -F "file=@docker-compose.yml" \
  "$BASE/api/compose/up?project_name=myapp"

# Tear down
curl -sf -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Requested-With: ContainerManager" \
  "$BASE/api/compose/down?project_name=myapp"
```

Compose files are validated before execution — `privileged`, host-path volumes, `secrets`, `build`, and other sandbox-escape keys are rejected with 400.

---

## Volumes and networks

```bash
# Volumes
curl -sf -H "Authorization: Bearer $TOKEN" $BASE/api/volumes
curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H "X-Requested-With: ContainerManager" \
  "$BASE/api/volumes/create?name=mydata"
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" -H "X-Requested-With: ContainerManager" \
  "$BASE/api/volumes/mydata"

# Networks
curl -sf -H "Authorization: Bearer $TOKEN" $BASE/api/networks
curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H "X-Requested-With: ContainerManager" \
  -H "Content-Type: application/json" \
  -d '{"driver": "bridge"}' \
  "$BASE/api/networks/create?name=mynet"
```

---

## System

```bash
# Engine info and disk usage
curl -sf -H "Authorization: Bearer $TOKEN" $BASE/api/system/info | python3 -m json.tool
curl -sf -H "Authorization: Bearer $TOKEN" $BASE/api/system/df   | python3 -m json.tool

# Prune stopped containers, unused images, dangling volumes
curl -sf -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Requested-With: ContainerManager" \
  $BASE/api/system/prune

# Prune build cache only
curl -sf -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Requested-With: ContainerManager" \
  $BASE/api/system/prune-build-cache
```

---

## Common errors

| HTTP | Meaning | Fix |
|---|---|---|
| 401 | Missing or wrong token | Check `Authorization: Bearer <token>` header |
| 403 | Missing CSRF header | Add `X-Requested-With: ContainerManager` |
| 400 (registry) | Image not in `ALLOWED_REGISTRIES` | Use an `us-docker.pkg.dev/…` path |
| 400 (compose) | Blocked key in compose file | Remove `privileged`, host mounts, `build`, etc. |
| 503 on `/ready` | SSH tunnel to Docker VM is down | Check `DOCKER_HOST`, SSH key, VM status |
| 500 on pull | Docker VM can't reach Artifact Registry | Run `gcloud auth configure-docker` on the VM |
