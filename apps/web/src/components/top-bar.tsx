import Link from "next/link";

export function TopBar() {
  return (
    <header className="topbar max-sm:flex-wrap max-sm:gap-x-2">
      <Link className="brand" href="/" aria-label="Refactra home">
        <span className="brand-mark" aria-hidden="true">r/</span>Refactra
      </Link>
      <nav className="topnav" aria-label="Main">
        <Link href="/">Workspace</Link>
        <Link href="/history">History</Link>
        <Link href="/privacy">Privacy</Link>
      </nav>
      <span className="environment"><span className="status-dot" />Local foundation <span>0.1</span></span>
    </header>
  );
}
