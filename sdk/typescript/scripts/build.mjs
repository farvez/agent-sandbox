// Builds dist/esm (ES modules) and dist/cjs (CommonJS) from src/ with the TypeScript compiler.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";

const tsc = createRequire(import.meta.url).resolve("typescript/bin/tsc");
const run = (...args) => execFileSync(process.execPath, [tsc, ...args], { stdio: "inherit" });

const pkg = JSON.parse(readFileSync("package.json", "utf8"));
const version = readFileSync("src/version.ts", "utf8").match(/"(.+)"/)[1];
if (version !== pkg.version) throw new Error(`src/version.ts says ${version} but package.json says ${pkg.version}`);

rmSync("dist", { recursive: true, force: true });
run("-p", "tsconfig.json");
run("-p", "tsconfig.json", "--module", "CommonJS", "--moduleResolution", "Node10", "--outDir", "dist/cjs");
mkdirSync("dist/cjs", { recursive: true });
writeFileSync("dist/cjs/package.json", JSON.stringify({ type: "commonjs" }) + "\n");
console.log(`Built airlock-sandbox ${version} (dist/esm, dist/cjs)`);
