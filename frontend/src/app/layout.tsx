import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Suspense } from "react";

import { SyntheticBanner } from "@/components/data/synthetic-banner";
import { SideNav } from "@/components/shell/side-nav";
import { SiteFooter } from "@/components/shell/site-footer";
import { TopBar } from "@/components/shell/top-bar";

import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "Market Pulse",
    template: "%s · Market Pulse",
  },
  description:
    "Energy-focused market intelligence: commodities, electricity, macro indicators, and company research.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:top-1 focus:left-1 focus:z-50 focus:border focus:border-accent focus:bg-panel-raised focus:px-3 focus:py-1.5 focus:text-xs focus:text-fg"
        >
          Skip to content
        </a>
        <TopBar />
        <Suspense fallback={null}>
          <SyntheticBanner />
        </Suspense>
        <div className="flex flex-1 flex-col lg:flex-row">
          <SideNav />
          <div className="flex min-w-0 flex-1 flex-col">
            <main id="main" tabIndex={-1} className="min-w-0 flex-1 p-3 focus:outline-none md:p-4">
              {children}
            </main>
            <SiteFooter />
          </div>
        </div>
      </body>
    </html>
  );
}
