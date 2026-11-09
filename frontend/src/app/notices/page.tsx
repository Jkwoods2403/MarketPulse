import type { Metadata } from "next";

import { Section } from "@/components/dashboard/section";
import { PageHeader } from "@/components/shell/page-header";

import notices from "./notices.json";

export const metadata: Metadata = { title: "Third-party notices" };

type Notice = (typeof notices.distributed)[number];

/**
 * Third-party notices, generated from installed package metadata and license files by
 * scripts/third-party-notices.mjs (`pnpm notices`; `pnpm notices:check` fails if stale). The
 * same content is in frontend/THIRD_PARTY_NOTICES.md. Texts are reproduced exactly.
 */
export default function NoticesPage() {
  const charts = notices.distributed.find((n) => n.name === "lightweight-charts");
  const chartsNotice = charts?.texts[0]?.text.trimEnd();
  return (
    <div className="space-y-3">
      <PageHeader
        title="Third-party notices"
        description="Open-source components this application distributes to your browser, with the licenses their packages declare and the license and notice texts they publish, reproduced exactly. This page records those texts; it is not a legal opinion."
      />

      {chartsNotice ? (
        <Section title="Charting attribution" meta="Lightweight Charts">
          <p className="text-xs leading-5 text-muted">
            Charts are rendered with TradingView Lightweight Charts™ (
            <a
              href="https://www.tradingview.com/"
              target="_blank"
              rel="noopener noreferrer"
              className="text-accent underline decoration-dotted"
            >
              https://www.tradingview.com/
            </a>
            ). Its NOTICE file:
          </p>
          <pre className="mt-2 overflow-x-auto border border-border bg-panel p-2 font-mono text-[11px] leading-4 text-fg">
            {chartsNotice}
          </pre>
        </Section>
      ) : null}

      <Section title="Distributed components" meta={`${notices.distributed.length} components`}>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[40rem] text-left text-xs leading-5">
            <caption className="sr-only">
              Components distributed to the browser, with version, declared license, and use
            </caption>
            <thead className="text-[11px] uppercase tracking-wider text-faint">
              <tr className="border-b border-border">
                <th scope="col" className="py-1 pr-3 font-medium">Component</th>
                <th scope="col" className="py-1 pr-3 font-medium">Version</th>
                <th scope="col" className="py-1 pr-3 font-medium">Declared license</th>
                <th scope="col" className="py-1 font-medium">Use</th>
              </tr>
            </thead>
            <tbody className="text-muted">
              {notices.distributed.map((n) => (
                <tr key={n.name} className="border-b border-border/60 last:border-b-0">
                  <th scope="row" className="py-1 pr-3 font-mono font-normal text-fg">
                    <a href={`#notice-${slug(n.name)}`} className="hover:underline">
                      {n.name}
                    </a>
                  </th>
                  <td className="py-1 pr-3 font-mono">{n.version}</td>
                  <td className="py-1 pr-3 font-mono">{n.license}</td>
                  <td className="py-1">{n.use}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="License and notice texts" meta="Verbatim">
        <div className="space-y-2">
          {notices.distributed.map((n) => (
            <NoticeBlock key={n.name} notice={n} />
          ))}
        </div>
      </Section>

      <Section title="Production dependency tree" meta={`${notices.inventory.length} packages`}>
        <p className="mb-2 max-w-3xl text-xs leading-5 text-muted">
          Every package in the production dependency tree with the license its package.json
          declares (<code className="font-mono">pnpm licenses list --prod</code>), including
          packages used only on the server or at build time. Development-only tools are not
          listed.
        </p>
        <details className="text-xs">
          <summary className="cursor-pointer text-muted hover:text-fg">
            Show all {notices.inventory.length} packages
          </summary>
          <table className="mt-2 w-full text-left leading-5">
            <caption className="sr-only">Production dependency tree with declared licenses</caption>
            <thead className="text-[11px] uppercase tracking-wider text-faint">
              <tr className="border-b border-border">
                <th scope="col" className="py-1 pr-3 font-medium">Package</th>
                <th scope="col" className="py-1 pr-3 font-medium">Versions</th>
                <th scope="col" className="py-1 font-medium">Declared license</th>
              </tr>
            </thead>
            <tbody className="font-mono text-muted">
              {notices.inventory.map((row) => (
                <tr key={row.name} className="border-b border-border/60 last:border-b-0">
                  <th scope="row" className="py-0.5 pr-3 font-normal">{row.name}</th>
                  <td className="py-0.5 pr-3">{row.versions.join(", ")}</td>
                  <td className="py-0.5">{row.license}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      </Section>

      <Section title="Platform-specific binary packages" meta={`${notices.platform.length} families`}>
        <p className="mb-2 max-w-3xl text-xs leading-5 text-muted">
          Optional dependencies of which each machine installs only the variant for its
          operating system and CPU; every variant in the lockfile is listed. {notices.platformNote}
        </p>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[40rem] text-left text-xs leading-5">
            <caption className="sr-only">
              Platform-specific binary package families with declared license, use, and variants
            </caption>
            <thead className="text-[11px] uppercase tracking-wider text-faint">
              <tr className="border-b border-border">
                <th scope="col" className="py-1 pr-3 font-medium">Family</th>
                <th scope="col" className="py-1 pr-3 font-medium">Versions</th>
                <th scope="col" className="py-1 pr-3 font-medium">Declared license</th>
                <th scope="col" className="py-1 font-medium">Use and variants</th>
              </tr>
            </thead>
            <tbody className="text-muted">
              {notices.platform.map((f) => (
                <tr key={f.family} className="border-b border-border/60 align-top last:border-b-0">
                  <th scope="row" className="py-1 pr-3 font-mono font-normal text-fg">{f.family}</th>
                  <td className="py-1 pr-3 font-mono">{f.versions.join(", ")}</td>
                  <td className="py-1 pr-3 font-mono">{f.license}</td>
                  <td className="py-1">
                    {f.use}{" "}
                    <span className="font-mono text-[11px] text-faint">{f.variants.join(", ")}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}

function slug(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function NoticeBlock({ notice }: { notice: Notice }) {
  return (
    <article id={`notice-${slug(notice.name)}`} className="border border-border bg-panel">
      <header className="border-b border-border px-3 py-2">
        <h3 className="text-xs font-medium text-fg">
          {notice.name} <span className="font-mono text-muted">{notice.version}</span>
        </h3>
        <p className="text-[11px] text-muted">
          Declared license: <span className="font-mono">{notice.license}</span>
        </p>
        {notice.copyright.length > 0 ? (
          <ul className="mt-1 text-[11px] text-muted">
            {notice.copyright.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        ) : null}
        {notice.note ? (
          <p className="mt-1 whitespace-pre-wrap text-[11px] text-faint">{notice.note}</p>
        ) : null}
      </header>
      {notice.texts.map((t) => (
        <details key={t.title} className="border-b border-border/60 px-3 py-1.5 last:border-b-0">
          <summary className="cursor-pointer text-[11px] text-muted hover:text-fg">{t.title}</summary>
          <pre className="mt-1.5 max-h-96 overflow-auto whitespace-pre-wrap font-mono text-[10px] leading-4 text-muted">
            {t.text}
          </pre>
        </details>
      ))}
    </article>
  );
}
