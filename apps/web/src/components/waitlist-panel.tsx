"use client";

import { ClockIcon } from "@primer/octicons-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { AuthControls } from "@/components/auth-controls";
import { accessNotice, type SessionStatus } from "@/lib/api";

export function WaitlistPanel() {
  const router = useRouter();
  const [session, setSession] = useState<SessionStatus | null>(null);

  // Invited users belong in the workspace, and signed-out visitors on the sign-in page.
  useEffect(() => {
    if (!session) return;
    if (!session.authenticated) router.replace("/sign-in");
    else if (!accessNotice(session)) router.replace("/");
  }, [session, router]);

  const removed = session?.authenticated && session.access === "denied";
  return <>
    <AuthControls session={session} onSessionChange={setSession} />
    {session?.authenticated && accessNotice(session) && <section className="waitlist-card" aria-labelledby="waitlist-title">
      <ClockIcon size={24} aria-hidden="true" className="waitlist-icon" />
      <h1 id="waitlist-title">{removed ? "Your account does not have access" : "You're on the waitlist"}</h1>
      <p>
        {removed
          ? "The owner has removed access for this GitHub account, so Refactra cannot run tasks for it."
          : <>Refactra is invite-only while it launches. <strong>{session.user.login}</strong> is on the
            list, and you will go straight to your workspace the next time you sign in after being invited.</>}
      </p>
      <p>You can <Link href="/privacy">delete your account and data</Link> at any time.</p>
    </section>}
  </>;
}
