import type { Metadata } from "next";
// Self-hosted OFL fonts: Mona Sans for interface text, Monaspace Neon for code.
import "@fontsource-variable/mona-sans";
import "@fontsource/monaspace-neon/400.css";
import "@fontsource/monaspace-neon/600.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Refactra · Local workspace",
  description: "A local foundation for approval-driven repository refactoring.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
