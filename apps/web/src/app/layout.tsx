import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Refactra · Local workspace",
  description: "A local foundation for approval-driven repository refactoring.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
