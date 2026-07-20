// A minimal static file server for the built Storybook.
//
// Node's own `http` rather than a dependency: this exists only so Playwright
// has something to point `webServer` at, and adding a package to serve a
// directory would be the largest thing in the devDependency list that nothing
// ships. Storybook's iframe loads ES modules, so `file://` is not an option —
// module scripts are blocked there by CORS.
import { createReadStream, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve, sep } from "node:path";

const ROOT = resolve(process.argv[2] ?? "storybook-static");
const PORT = Number(process.env.STORYBOOK_PORT ?? 6007);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".map": "application/json; charset=utf-8",
};

createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://localhost");
  // `decodeURIComponent` THROWS on a malformed escape — `GET /%` is enough. This
  // handler is the top of the stack, so an uncaught throw here does not fail one
  // request, it kills the server process and every remaining test in the run
  // fails on a dead port, pointing at whatever spec happened to be next rather
  // than at the request that did it. A bad percent-escape is a bad request.
  let requested;
  try {
    requested = decodeURIComponent(url.pathname);
  } catch {
    res.writeHead(400).end("bad request");
    return;
  }
  // `normalize` then verify the result is still inside ROOT: a request for
  // `/../../etc/passwd` must not escape the served directory even though this
  // only ever runs locally and in CI.
  const target = resolve(join(ROOT, normalize(requested)));
  if (target !== ROOT && !target.startsWith(ROOT + sep)) {
    res.writeHead(403).end("forbidden");
    return;
  }

  let file = target;
  try {
    if (statSync(file).isDirectory()) {
      file = join(file, "index.html");
    }
  } catch {
    res.writeHead(404).end("not found");
    return;
  }

  try {
    statSync(file);
  } catch {
    res.writeHead(404).end("not found");
    return;
  }

  res.writeHead(200, {
    "content-type": TYPES[extname(file)] ?? "application/octet-stream",
    "cache-control": "no-store",
  });
  createReadStream(file).pipe(res);
}).listen(PORT, () => {
  console.log(`storybook-static on http://127.0.0.1:${PORT}`);
});
