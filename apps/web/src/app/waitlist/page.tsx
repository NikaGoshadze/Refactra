import type { Metadata } from "next";
import { TopBar } from "@/components/top-bar";
import { WaitlistPanel } from "@/components/waitlist-panel";

export const metadata: Metadata = { title: "Waitlist · Refactra" };

export default function WaitlistPage() {
  return (
    <div className="workspace">
      <TopBar />
      <main><WaitlistPanel /></main>
    </div>
  );
}
