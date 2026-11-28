"use client";

import { CheckIcon, MarkGithubIcon, XIcon } from "@primer/octicons-react";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { accessNotice, request, type SessionStatus } from "@/lib/api";
import { loginHref } from "@/lib/navigation";

export function SignInCard({ next }: { next: string }) {
  const router = useRouter();

  // Someone already signed in (for example from a bookmark) goes straight on.
  useEffect(() => {
    const controller = new AbortController();
    request<SessionStatus>("auth/session", { signal: controller.signal })
      .then((session) => {
        if (controller.signal.aborted || !session.authenticated) return;
        router.replace(accessNotice(session) ? "/waitlist" : next);
      })
      .catch(() => { /* The sign-in button works without this check. */ });
    return () => controller.abort();
  }, [next, router]);

  return (
    <section className="signin-card" aria-labelledby="signin-card-title">
      <h2 id="signin-card-title">Sign in to Refactra</h2>
      <p className="signin-card-copy">Refactra uses your GitHub account. There is no separate password.</p>
      {/* A plain link: OAuth needs full navigation without prefetching. */}
      <a className="signin-button" href={loginHref(next)}>
        <MarkGithubIcon size={20} aria-hidden="true" />Sign in with GitHub
      </a>
      <h3>Refactra can</h3>
      <ul className="signin-scope signin-can">
        <li><CheckIcon size={16} aria-hidden="true" />Read only the repositories you install the Refactra GitHub App on</li>
        <li><CheckIcon size={16} aria-hidden="true" />Push a new branch and open a draft pull request, after you approve both the plan and the change</li>
      </ul>
      <h3>Refactra cannot</h3>
      <ul className="signin-scope signin-cannot">
        <li><XIcon size={16} aria-hidden="true" />See repositories you have not installed it on</li>
        <li><XIcon size={16} aria-hidden="true" />Change your existing branches or merge pull requests</li>
      </ul>
      <p className="signin-note">
        New accounts join a waitlist until they are invited. You can delete your account and data at any
        time on the <a href="/privacy">privacy page</a>.
      </p>
    </section>
  );
}
