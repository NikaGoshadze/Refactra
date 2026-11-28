import type { Metadata } from "next";
import Link from "next/link";
import { CheckCircleFillIcon, GitPullRequestDraftIcon } from "@primer/octicons-react";
import { SignInCard } from "@/components/sign-in-card";
import { safeNext } from "@/lib/navigation";

export const metadata: Metadata = { title: "Sign in · Refactra" };

type Line = { number: number; text: string } | null;
type Row = { kind: "context" | "change"; old: Line; new: Line };

// A real hunk from a Refactra run on AI-Hub: pull request #31, pizza_app/main.py. D3 replaces
// this static rendering with the shared diff viewer.
const ROWS: Row[] = [
  { kind: "context", old: { number: 104, text: "if is_summary_command(user_input):" }, new: { number: 106, text: "if is_summary_command(user_input):" } },
  { kind: "change", old: { number: 105, text: "    current_summary = memory.moving_summary_buffer" }, new: { number: 107, text: "    for line in summary_lines(memory.moving_summary_buffer):" } },
  { kind: "change", old: { number: 106, text: "    if not current_summary:" }, new: { number: 108, text: "        print(line)" } },
  { kind: "change", old: { number: 107, text: "        print(\"\\n[System: ჯერ საუბრის ისტორია არ არის.]\")" }, new: null },
  { kind: "change", old: { number: 108, text: "    else:" }, new: null },
  { kind: "change", old: { number: 109, text: "        print(\"\\n--- მიმდინარე საუბრის ისტორია ---\")" }, new: null },
  { kind: "change", old: { number: 110, text: "        print(current_summary)" }, new: null },
  { kind: "context", old: { number: 111, text: "    continue" }, new: { number: 109, text: "    continue" } },
];

function Side({ line, kind, sign }: { line: Line; kind: Row["kind"]; sign: "-" | "+" }) {
  const tone = !line ? "diff-empty" : kind === "context" ? "" : sign === "-" ? "diff-del" : "diff-add";
  return <>
    <td className={`diff-num ${tone}`}>{line?.number}</td>
    <td className={`diff-code ${tone}`}>
      {line && <><span className="diff-sign" aria-hidden="true">{kind === "context" ? " " : sign}</span>{line.text}</>}
    </td>
  </>;
}

function SampleDiff() {
  const unified = [
    ...ROWS.filter((row) => row.kind === "context").slice(0, 1).map((row) => ({ sign: " ", old: row.old, new: row.new })),
    ...ROWS.filter((row) => row.kind === "change" && row.old).map((row) => ({ sign: "-", old: row.old, new: null })),
    ...ROWS.filter((row) => row.kind === "change" && row.new).map((row) => ({ sign: "+", old: null, new: row.new })),
    ...ROWS.filter((row) => row.kind === "context").slice(1).map((row) => ({ sign: " ", old: row.old, new: row.new })),
  ];
  return (
    <figure className="sample-diff" aria-label="Example change from a Refactra run">
      <div className="diff-file">
        <span className="diff-path">pizza_app/main.py</span>
        <span className="diff-stat"><span className="diff-stat-add">+4</span> <span className="diff-stat-del">−6</span></span>
      </div>
      <div className="diff-hunk">@@ -102,12 +104,8 @@ def run_chatbot():</div>
      <div className="diff-scroll">
        <table className="diff-table diff-split">
          <tbody>{ROWS.map((row, index) => <tr key={index}>
            <Side line={row.old} kind={row.kind} sign="-" /><Side line={row.new} kind={row.kind} sign="+" />
          </tr>)}</tbody>
        </table>
        <table className="diff-table diff-unified">
          <tbody>{unified.map((row, index) => {
            const tone = row.sign === "-" ? "diff-del" : row.sign === "+" ? "diff-add" : "";
            return <tr key={index}>
              <td className={`diff-num ${tone}`}>{row.old?.number}</td>
              <td className={`diff-num ${tone}`}>{row.new?.number}</td>
              <td className={`diff-code ${tone}`}><span className="diff-sign" aria-hidden="true">{row.sign}</span>{(row.old ?? row.new)?.text}</td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      <figcaption className="diff-footer">
        <span className="diff-checks"><CheckCircleFillIcon size={16} aria-hidden="true" />3 new tests passed in the sandbox</span>
        <span className="diff-pr"><GitPullRequestDraftIcon size={16} aria-hidden="true" />AI-Hub #31 · Extract summary rendering into a helper</span>
      </figcaption>
    </figure>
  );
}

export default async function SignInPage({ searchParams }: { searchParams: Promise<{ next?: string | string[] }> }) {
  const { next } = await searchParams;
  const target = safeNext(typeof next === "string" ? next : null);
  return (
    <div className="workspace signin-page">
      <header className="signin-top">
        <Link className="brand" href="/sign-in" aria-label="Refactra">
          <span className="brand-mark" aria-hidden="true">r/</span>Refactra
        </Link>
        <Link className="signin-privacy" href="/privacy">Privacy</Link>
      </header>
      <main className="signin-grid">
        <section className="signin-pitch" aria-labelledby="signin-title">
          <h1 id="signin-title">Refactoring you review before it reaches GitHub.</h1>
          <p className="signin-copy">
            Describe a change. Refactra reads the repository, proposes a plan, writes the code and runs
            the tests in a sandbox. You approve the plan and the pull request.
          </p>
          <SampleDiff />
        </section>
        <SignInCard next={target} />
      </main>
    </div>
  );
}
