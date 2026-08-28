import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function run(cmd, args, extra = {}) {
  const child = spawn(cmd, args, {
    cwd: root,
    stdio: "inherit",
    env: process.env,
    ...extra,
  });
  child.on("exit", (code) => {
    if (code && code !== 0) process.exit(code ?? 1);
  });
  return child;
}

const py = process.env.PYTHON || "python3";
const api = run(py, ["-m", "kline_match", "serve", "--host", "127.0.0.1", "--port", "18765"]);
const web = run("npm", ["run", "dev"], { cwd: path.join(root, "web") });

function shutdown() {
  api.kill("SIGTERM");
  web.kill("SIGTERM");
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
