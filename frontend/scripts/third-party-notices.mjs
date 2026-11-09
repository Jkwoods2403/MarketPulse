// Generates the third-party notices from installed package metadata and license files, plus the
// verbatim upstream files in third-party/upstream/ (for packages that do not ship their own).
//
//   node scripts/third-party-notices.mjs          write THIRD_PARTY_NOTICES.md and
//                                                  src/app/notices/notices.json
//   node scripts/third-party-notices.mjs --check  exit 1 if either file is out of date
//
// No dependencies. Package versions come from each installed package.json; license texts are
// copied exactly from the files named below. Nothing here is a legal conclusion: it records
// what each package declares and the texts it (or its upstream repository) publishes.

import { execSync } from "node:child_process";
import { existsSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const UPSTREAM = join(ROOT, "third-party", "upstream");
const MD_OUT = join(ROOT, "THIRD_PARTY_NOTICES.md");
const JSON_OUT = join(ROOT, "src", "app", "notices", "notices.json");
const FENCE = "```";

/** Find an installed package the way Node resolves it from `fromDir` (pnpm layout aware). */
function packageDir(name, fromDir) {
  let dir = realpathSync(fromDir);
  for (;;) {
    const candidate =
      basename(dir) === "node_modules" ? join(dir, name) : join(dir, "node_modules", name);
    if (existsSync(join(candidate, "package.json"))) return realpathSync(candidate);
    const parent = dirname(dir);
    if (parent === dir) throw new Error(`package not found: ${name} (from ${fromDir})`);
    dir = parent;
  }
}

const read = (file) => readFileSync(file, "utf8").replace(/\r\n/g, "\n");
const pkgJson = (dir) => JSON.parse(read(join(dir, "package.json")));

function copyrightLines(text) {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => /^Copyright (\(|©|\d)/.test(line));
}

// Components distributed to the browser (client JavaScript bundle or self-hosted font files).
// `via` is the package the component is resolved from (its dependent); null: the app itself.
const COMPONENTS = [
  {
    name: "lightweight-charts",
    via: null,
    use: "Historical charts (client bundle).",
    texts: (dir) => [
      {
        title: "NOTICE (upstream repository, tag v5.2.1; the npm package has no NOTICE file)",
        file: join(UPSTREAM, "lightweight-charts-5.2.1-NOTICE"),
      },
      { title: "LICENSE (npm package)", file: join(dir, "LICENSE") },
    ],
    note: (dir) => {
      const head = read(join(dir, "dist", "lightweight-charts.production.mjs")).slice(0, 400);
      const banner = head.match(/\/\*![\s\S]*?\*\//)?.[0];
      return banner ? `Banner of dist/lightweight-charts.production.mjs:\n${banner}` : null;
    },
  },
  {
    name: "fancy-canvas",
    via: "lightweight-charts",
    use: "Canvas helper used by Lightweight Charts (client bundle).",
    texts: () => [
      {
        title:
          "LICENSE (upstream repository, tag 2.1.0; the npm package declares MIT but includes no license file)",
        file: join(UPSTREAM, "fancy-canvas-2.1.0-LICENSE"),
      },
    ],
  },
  {
    name: "next",
    via: null,
    use: "Application framework; its client runtime is in the client bundle.",
    texts: (dir) => [{ title: "license.md (npm package)", file: join(dir, "license.md") }],
    note: () =>
      "Next.js bundles further third-party code in next/dist/compiled/, each with its own " +
      "license file inside the next package; those files are not reproduced here.",
  },
  {
    name: "react",
    via: null,
    use: "UI library (client bundle).",
    texts: (dir) => [{ title: "LICENSE (npm package)", file: join(dir, "LICENSE") }],
  },
  {
    name: "react-dom",
    via: null,
    use: "UI library (client bundle).",
    texts: (dir) => [{ title: "LICENSE (npm package)", file: join(dir, "LICENSE") }],
  },
  {
    name: "scheduler",
    via: "react-dom",
    use: "React scheduler (client bundle).",
    texts: (dir) => [{ title: "LICENSE (npm package)", file: join(dir, "LICENSE") }],
  },
  {
    name: "@swc/helpers",
    via: "next",
    use:
      "Compiler runtime helpers Next.js can include in client code (listed as a precaution: " +
      "its presence in the minified client bundle could not be confirmed).",
    texts: (dir) => [{ title: "LICENSE (npm package)", file: join(dir, "LICENSE") }],
  },
];

const FONTS = [
  {
    name: "Geist and Geist Mono (fonts)",
    version: "as served by Google Fonts at build time",
    license: "OFL-1.1",
    use: "Interface fonts, downloaded by next/font/google at build time and served from this application.",
    texts: [
      {
        title: "OFL.txt (google/fonts repository, ofl/geist; identical to ofl/geistmono)",
        file: join(UPSTREAM, "geist-OFL.txt"),
      },
    ],
  },
];

function resolveComponent(c) {
  const from = c.via === null ? ROOT : packageDir(c.via, ROOT);
  const dir = packageDir(c.name, from);
  const pkg = pkgJson(dir);
  const texts = c.texts(dir).map((t) => ({ title: t.title, text: read(t.file) }));
  return {
    name: c.name,
    version: pkg.version,
    license: pkg.license,
    use: c.use,
    copyright: [...new Set(texts.flatMap((t) => copyrightLines(t.text)))],
    note: c.note ? c.note(dir) : null,
    texts,
  };
}

function resolveFont(f) {
  const texts = f.texts.map((t) => ({ title: t.title, text: read(t.file) }));
  return {
    name: f.name,
    version: f.version,
    license: f.license,
    use: f.use,
    copyright: [...new Set(texts.flatMap((t) => copyrightLines(t.text)))],
    note: null,
    texts,
  };
}

// Platform-specific binary packages (optional dependencies with os/cpu/libc constraints): each
// machine installs only its own variant, so they are listed by family from the lockfile (the
// same on every OS), not from what happens to be installed. The declared license of each family
// is recorded here and verified against whichever variants are installed on the machine that
// runs this script (CI runs it on Linux; developers on Windows or macOS).
const PLATFORM_FAMILIES = [
  {
    pattern: /^@next\/swc-/,
    family: "@next/swc-*",
    license: "MIT",
    use: "Next.js native compiler, used at build time and by the Next.js server.",
  },
  {
    pattern: /^@img\/sharp-libvips-/,
    family: "@img/sharp-libvips-*",
    license: "LGPL-3.0-or-later",
    use: "libvips binaries for sharp on macOS and Linux.",
  },
  {
    pattern: /^@img\/sharp-win32-/,
    family: "@img/sharp-win32-*",
    license: "Apache-2.0 AND LGPL-3.0-or-later",
    use: "sharp for Windows, with libvips bundled.",
  },
  {
    pattern: /^@img\/sharp-/,
    family: "@img/sharp-* (macOS, Linux, WebAssembly)",
    license: "Apache-2.0",
    use: "sharp native bindings.",
  },
];
const SHARP_NOTE =
  "sharp and its binaries are used only by Next.js image optimization (next/image), which this " +
  "application does not use; they are not distributed to the browser.";

/** Lockfile packages with os/cpu/libc constraints: [{ name, version }]. */
function lockfilePlatformPackages() {
  // pnpm 12 lockfiles can hold several YAML documents (pnpm's own executables, then the
  // project), each with its own `packages:` section.
  const lock = read(join(ROOT, "pnpm-lock.yaml"));
  const sections = lock
    .split(/^---$/m)
    .map((doc) => doc.split("\npackages:\n")[1]?.split(/\n(?:snapshots|importers):\n/)[0])
    .filter(Boolean);
  if (sections.length === 0) throw new Error("pnpm-lock.yaml: no packages section");
  const found = [];
  for (const block of sections.join("\n").split(/\n(?=  \S)/)) {
    const key = block.match(/^ {2}'?([^\s']+?)'?:/)?.[1];
    if (!key || !/^ {4}(os|cpu|libc):/m.test(block)) continue;
    const at = key.lastIndexOf("@");
    found.push({ name: key.slice(0, at), version: key.slice(at + 1) });
  }
  return found;
}

function inventory() {
  const out = execSync("pnpm licenses list --prod --json", { cwd: ROOT, encoding: "utf8" });
  const platformNames = new Set(lockfilePlatformPackages().map((p) => p.name));
  const rows = [];
  const installedPlatform = [];
  for (const [license, pkgs] of Object.entries(JSON.parse(out))) {
    for (const p of pkgs) {
      const row = { name: p.name, versions: [...p.versions].sort(), license };
      (platformNames.has(p.name) ? installedPlatform : rows).push(row);
    }
  }
  // Every platform-specific package in the production tree must belong to a known family whose
  // recorded license matches what the installed variant declares.
  for (const p of installedPlatform) {
    const family = PLATFORM_FAMILIES.find((f) => f.pattern.test(p.name));
    if (!family) {
      throw new Error(`${p.name}: platform-specific package without a family in PLATFORM_FAMILIES`);
    }
    if (family.license !== p.license) {
      throw new Error(
        `${p.name} declares "${p.license}" but PLATFORM_FAMILIES records "${family.license}" for ${family.family}`,
      );
    }
  }
  return {
    rows: rows.sort((a, b) => a.name.localeCompare(b.name)),
    platform: platformFamilies(),
    verified: installedPlatform.map((p) => `${p.name} (${p.license})`).sort(), // logged only
  };
}

/** The platform families with every variant in the lockfile (deterministic across OSes). */
function platformFamilies() {
  const variants = lockfilePlatformPackages();
  return PLATFORM_FAMILIES.map((f) => {
    const members = variants
      .filter((v) => f.pattern.test(v.name) && PLATFORM_FAMILIES.find((g) => g.pattern.test(v.name)) === f)
      .sort((a, b) => a.name.localeCompare(b.name));
    return {
      family: f.family,
      license: f.license,
      use: f.use,
      versions: [...new Set(members.map((m) => m.version))].sort(),
      variants: members.map((m) => m.name),
    };
  }).filter((f) => f.variants.length > 0);
}

function markdown({ distributed, inventory: rows, platform }) {
  const lines = [
    "# Third-party notices",
    "",
    "<!-- Generated by scripts/third-party-notices.mjs (pnpm notices). Do not edit by hand. -->",
    "",
    "Market Pulse's frontend distributes the components below to the browser (in the client",
    "JavaScript bundle, or as self-hosted font files). For each, the license its package declares",
    "and the license and notice texts it publishes are reproduced exactly. This document records",
    "those texts; it is not a legal opinion.",
    "",
    "Charts are rendered with TradingView Lightweight Charts™ (https://www.tradingview.com/).",
    "",
    "| Component | Version | Declared license | Use |",
    "|---|---|---|---|",
    ...distributed.map((c) => `| ${c.name} | ${c.version} | ${c.license} | ${c.use} |`),
    "",
  ];
  for (const c of distributed) {
    lines.push(`## ${c.name} ${c.version}`, "", `Declared license: ${c.license}. ${c.use}`, "");
    if (c.copyright.length) {
      lines.push("Copyright lines in the texts below:", "", ...c.copyright.map((l) => `- ${l}`), "");
    }
    if (c.note) lines.push(c.note.includes("\n") ? `${FENCE}text\n${c.note}\n${FENCE}` : c.note, "");
    for (const t of c.texts) lines.push(`### ${t.title}`, "", `${FENCE}text`, t.text.trimEnd(), FENCE, "");
  }
  lines.push(
    "## Production dependency tree",
    "",
    "Every package in the production dependency tree with the license its package.json declares",
    "(`pnpm licenses list --prod`), including packages used only on the server or at build time.",
    "Platform-specific binary packages are listed separately below. Development-only tools are not",
    "listed.",
    "",
    "| Package | Versions | Declared license |",
    "|---|---|---|",
    ...rows.map((r) => `| ${r.name} | ${r.versions.join(", ")} | ${r.license} |`),
    "",
    "## Platform-specific binary packages",
    "",
    "Optional dependencies of which each machine installs only the variant for its operating",
    "system and CPU. Every variant in the lockfile is listed; the declared license of each family",
    "is checked against the installed variants whenever these notices are generated or checked.",
    SHARP_NOTE,
    "",
    "| Family | Versions | Declared license | Use | Variants in the lockfile |",
    "|---|---|---|---|---|",
    ...platform.map(
      (f) => `| ${f.family} | ${f.versions.join(", ")} | ${f.license} | ${f.use} | ${f.variants.join(", ")} |`,
    ),
    "",
  );
  return lines.join("\n");
}

const tree = inventory();
const data = {
  distributed: [...COMPONENTS.map(resolveComponent), ...FONTS.map(resolveFont)],
  inventory: tree.rows,
  platform: tree.platform,
  platformNote: SHARP_NOTE,
};
console.log(`Platform-specific licenses verified on this machine: ${tree.verified.join("; ") || "(none installed)"}`);
const outputs = [
  [MD_OUT, markdown(data)],
  [JSON_OUT, `${JSON.stringify(data, null, 2)}\n`],
];

if (process.argv.includes("--check")) {
  const stale = outputs.filter(([file, text]) => !existsSync(file) || read(file) !== text);
  if (stale.length) {
    console.error(`Out of date: ${stale.map(([f]) => f).join(", ")}. Run: pnpm notices`);
    process.exit(1);
  }
  console.log("Third-party notices are up to date.");
} else {
  for (const [file, text] of outputs) {
    writeFileSync(file, text);
    console.log(`wrote ${file}`);
  }
}
