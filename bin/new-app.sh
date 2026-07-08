#!/usr/bin/env bash
# new-app.sh <name> [--static|--node|--swift|--board] [--dir <path>]
#
# Nothing → live at https://<name>.jimmyhoughjr.net, one command:
#   1. dokku app + domain
#   2. scaffold a repo from a template (default: --static)
#   3. git init, commit, push (first deploy)
#   4. publish the Cloudflare route via API (publish-route.sh)
#   5. verify LAN + public
#
# Templates:
#   --static  nginx serving index.html            (deploys in ~15 s)
#   --node    zero-dep node http server, /health  (deploys in ~30 s)
#   --swift   Hummingbird 2 hello, /health        (first pi build ~8 min)
#   --board   statusgen status board              (deploys in ~15 s)
#
# Requires: dokku@ key auth to the pi, and ~/.cf_api_token (see publish-route.sh).
set -euo pipefail

DOKKU="dokku@192.168.0.103"
DOMAIN="jimmyhoughjr.net"
BIN="$(cd "$(dirname "$0")" && pwd)"

NAME="" KIND="static" DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --static|--node|--swift|--board) KIND="${1#--}" ;;
    --dir) DIR="$2"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) NAME="$1" ;;
  esac
  shift
done
[[ -n "$NAME" ]] || { echo "usage: new-app.sh <name> [--static|--node|--swift|--board]" >&2; exit 1; }
[[ "$NAME" =~ ^[a-z0-9-]{1,32}$ ]] || { echo "error: name must be [a-z0-9-]" >&2; exit 1; }
DIR="${DIR:-$HOME/${NAME}-site}"
[[ -e "$DIR" ]] && { echo "error: $DIR already exists" >&2; exit 1; }

FQDN="${NAME}.${DOMAIN}"
echo "==> dokku app '${NAME}' + domain ${FQDN}"
ssh "$DOKKU" apps:create "$NAME" 2>&1 | grep -v "^$" | tail -1 || true
ssh "$DOKKU" domains:set "$NAME" "$FQDN" > /dev/null

echo "==> scaffolding ${KIND} app in ${DIR}"
mkdir -p "$DIR"
case "$KIND" in
  static)
    printf 'FROM nginx:alpine\nCOPY . /usr/share/nginx/html\n' > "$DIR/Dockerfile"
    printf '.git\n' > "$DIR/.dockerignore"
    cat > "$DIR/index.html" <<HTML
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<title>${NAME}</title>
<style>
  :root { --bg:#F7F6F2; --ink:#23211C; --muted:#6F6B62; --accent:#B4551F; }
  @media (prefers-color-scheme: dark) { :root { --bg:#1A1C1F; --ink:#E8E6E1; --muted:#9A968D; --accent:#E07A3F; } }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink);
         font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { text-align:center; padding:2rem; }
  h1 { font-size:2rem; margin:0 0 .5rem; }
  p { color:var(--muted); margin:0; }
  code { color:var(--accent); }
</style>
</head>
<body>
<main>
  <svg width="72" height="72" viewBox="0 0 64 64" fill="currentColor" style="color:var(--accent)" aria-label="Roost"><path d="M19 39 C5 34 2 18 9 7 C9.5 21 14 31 24 33 Z"/><path d="M22 33 C13 29 11 19 15 11 C16 21 19 27 27 29 Z"/><ellipse cx="29" cy="33" rx="13" ry="11"/><path d="M35 17 L45 25 L36 33 L26 25 Z"/><path fill-rule="evenodd" d="M37.3 18 a5.7 5.7 0 1 0 11.4 0 a5.7 5.7 0 1 0 -11.4 0 Z M43.2 16.8 a1.4 1.4 0 1 0 2.8 0 a1.4 1.4 0 1 0 -2.8 0 Z"/><circle cx="40.5" cy="11" r="2.9"/><circle cx="45" cy="11.8" r="2.4"/><path d="M48.2 15.8 L55 18.2 L48.2 20.8 Z"/><rect x="26.6" y="42" width="2.6" height="10" rx="1.3"/><rect x="32.8" y="42" width="2.6" height="10" rx="1.3"/><rect x="7" y="50.5" width="50" height="3" rx="1.5"/></svg>
  <h1>${NAME}</h1>
  <p>This page went from nothing to live in one command:<br><code>new-app.sh ${NAME} --static</code></p>
  <p style="margin-top:.5rem">Edit <code>index.html</code>, then <code>git push dokku main</code>.</p>
  <p style="margin-top:1rem;font-size:.85rem"><a href="https://docs.jimmyhoughjr.net" style="color:var(--accent);text-decoration:none">Roost docs</a> &middot; <a href="https://github.com/jhoughjr/roost" style="color:var(--accent);text-decoration:none">source</a></p>
</main>
</body>
</html>
HTML
    cp "$BIN/../assets/favicon.svg" "$DIR/favicon.svg" 2>/dev/null || true
    ;;
  node)
    printf 'FROM node:22-alpine\nWORKDIR /app\nCOPY server.js .\nEXPOSE 80\nCMD ["node", "server.js"]\n' > "$DIR/Dockerfile"
    printf '.git\n' > "$DIR/.dockerignore"
    cat > "$DIR/server.js" <<'JS'
const http = require("http");
http.createServer((req, res) => {
  if (req.url === "/health") { res.setHeader("content-type", "application/json"); return res.end('{"ok":true}'); }
  res.setHeader("content-type", "text/plain");
  res.end("hello from a new-app.sh node app\n");
}).listen(process.env.PORT || 80, () => console.log("up"));
JS
    ;;
  swift)
    mkdir -p "$DIR/Sources/App"
    cat > "$DIR/Package.swift" <<'SWIFT'
// swift-tools-version:6.0
import PackageDescription
let package = Package(
    name: "app",
    platforms: [.macOS(.v14)],
    dependencies: [.package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.5.0")],
    targets: [.executableTarget(name: "App", dependencies: [.product(name: "Hummingbird", package: "hummingbird")], path: "Sources/App")]
)
SWIFT
    cat > "$DIR/Sources/App/App.swift" <<'SWIFT'
import Hummingbird

@main
struct App {
    static func main() async throws {
        let router = Router()
        router.get("health") { _, _ in #"{"ok":true}"# }
        router.get("") { _, _ in "hello from a new-app.sh swift app\n" }
        let app = Application(router: router,
                              configuration: .init(address: .hostname("0.0.0.0", port: 80)))
        try await app.runService()
    }
}
SWIFT
    cat > "$DIR/Dockerfile" <<'DOCKER'
FROM swift:6.1-noble AS build
WORKDIR /build
COPY Package.swift Package.resolved* ./
RUN swift package resolve
COPY Sources ./Sources
RUN swift build -c release --static-swift-stdlib && cp .build/release/App /app-bin

FROM ubuntu:noble
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /app-bin /usr/local/bin/app
EXPOSE 80
CMD ["app"]
DOCKER
    printf '.build/\n.swiftpm/\n' > "$DIR/.gitignore"
    if command -v swift > /dev/null; then
      echo "==> local swift build (type-check before the slow pi build; Mac-clean ≠ pi-clean, but catches most)"
      (cd "$DIR" && swift build 2>&1 | tail -2)
    fi
    ;;
  board)
    printf 'FROM nginx:alpine\nCOPY . /usr/share/nginx/html\n' > "$DIR/Dockerfile"
    printf '.git\n_assets/\n' > "$DIR/.dockerignore"
    mkdir -p "$DIR/_assets"
    # TODO: copy board.js and board.css from statusgen repo
    # For now, this template scaffolds the structure; see playbook §7
    cat > "$DIR/board.json" <<'JSON'
{
  "title": "${NAME}",
  "eyebrow": "${FQDN}",
  "stamp": "Updated $(date +'%Y-%m-%d') — edit board.json to add your status sections",
  "sections": [
    {
      "kind": "stats",
      "items": [
        { "n": "1", "label": "Status board", "tone": "go" },
        { "n": "0", "label": "Issues blocking", "tone": "go" }
      ]
    },
    {
      "kind": "banner",
      "tone": "none",
      "text": "Welcome to your statusgen board. Edit <code>board.json</code> to add sections: stats, cards, tables, charts. See <a href=\"https://github.com/jhoughjr/statusgen/blob/main/BOARD_SCHEMA.md\">BOARD_SCHEMA.md</a> for the data model. Deploy with <code>git push dokku main</code>."
    }
  ]
}
JSON
    cat > "$DIR/index.html" <<'HTML'
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>PLACEHOLDER</title>
  <!-- TODO: update board.css + board.js paths to match your setup (e.g., from statusgen repo or a CDN) -->
  <!-- For now, see playbook §7 for full setup steps. -->
  <link rel="stylesheet" href="/_assets/board.css" />
</head>
<body>
  <div class="wrap" id="board-root">
    <p class="board-loading">Loading board…</p>
  </div>

  <script>window.BOARD_SRC = "board.json";</script>
  <script src="/_assets/board.js"></script>
</body>
</html>
HTML
    ;;
esac

echo "==> first deploy"
cd "$DIR"
git init -q -b main
git add -A
git commit -q -m "scaffold ${NAME} (${KIND}) via new-app.sh"
git remote add dokku "${DOKKU}:${NAME}"
[[ "$KIND" == "swift" ]] && echo "    (swift on the pi: first build takes ~8 min — hang tight)"
git push dokku main 2>&1 | tail -2

echo "==> publishing route"
"$BIN/publish-route.sh" "$NAME"

echo "==> verifying"
curl -s -o /dev/null -w "    LAN:    %{http_code}\n" -m 10 -H "Host: ${FQDN}" http://192.168.0.103/
for i in 1 2 3 4 5 6; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "https://${FQDN}/" || true)
  [[ "$CODE" == "200" ]] && break
  sleep 10
done
echo "    public: ${CODE:-000}"

cat <<EOF

✓ https://${FQDN} is live
  repo:    ${DIR}
  deploy:  git push dokku main
  next:    persistent data → dokku storage:mount (playbook §3)
           cron            → app.json (playbook §4)
           accounts        → add origin to vault ALLOWED_ORIGINS (playbook §6)
EOF
if [[ "$KIND" != "board" ]]; then
  cat <<EOF2
           status board    → statusgen new-board.sh (playbook §7)
EOF2
else
  cat <<EOF2
           board setup     → copy _assets/{board.js,board.css} from statusgen (playbook §7)
           then edit       → board.json to add your sections + git push dokku main
EOF2
fi
