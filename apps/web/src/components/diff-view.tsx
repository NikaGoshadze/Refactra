"use client";

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import type { HighlightedLines } from "@/lib/diff-highlighting";

export type CodeLine = { kind: "add" | "del" | "context"; text: string; old?: number; new?: number };
type Marker = { kind: "hunk"; text: string } | { kind: "note"; text: string };
export type DiffFile = { path: string; lines: (CodeLine | Marker)[]; added: number; removed: number };

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;
const NO_NEWLINE = "\\ No newline at end of file";

/** Parse the unified diff saved by the worker. Hunk lengths distinguish edits from file headers. */
function parseDiff(diff: string): DiffFile[] {
  const rows = diff.replace(/\n$/, "").split("\n");
  const files: DiffFile[] = [];
  let file: DiffFile | undefined;
  let oldLine = 0, newLine = 0, oldLeft = 0, newLeft = 0;

  for (let index = 0; index < rows.length; index++) {
    const row = rows[index].replace(/\r$/, "");
    if (file && row === NO_NEWLINE) {
      file.lines.push({ kind: "note", text: row });
    } else if (file && (oldLeft > 0 || newLeft > 0)) {
      const mark = row[0];
      if (mark !== " " && mark !== "+" && mark !== "-") return [];
      if ((mark !== "+" && oldLeft === 0) || (mark !== "-" && newLeft === 0)) return [];
      const line: CodeLine = { kind: mark === "+" ? "add" : mark === "-" ? "del" : "context", text: row.slice(1) };
      if (mark !== "+") { line.old = oldLine++; oldLeft--; }
      if (mark !== "-") { line.new = newLine++; newLeft--; }
      if (mark === "+") file.added++;
      if (mark === "-") file.removed++;
      file.lines.push(line);
    } else if (row.startsWith("--- ") && rows[index + 1]?.startsWith("+++ ")) {
      const oldPath = row.slice(4).replace(/^a\//, "");
      const newPath = rows[++index].slice(4).replace(/\r$/, "").replace(/^b\//, "");
      file = { path: newPath === "/dev/null" ? oldPath : newPath, lines: [], added: 0, removed: 0 };
      files.push(file);
    } else {
      const hunk = HUNK.exec(row);
      if (!file || !hunk) return [];
      oldLine = Number(hunk[1]);
      oldLeft = Number(hunk[2] ?? 1);
      newLine = Number(hunk[3]);
      newLeft = Number(hunk[4] ?? 1);
      file.lines.push({ kind: "hunk", text: row });
    }
  }
  return oldLeft === 0 && newLeft === 0 ? files : [];
}

type SplitRow = Marker | { kind: "code"; old?: CodeLine; new?: CodeLine };

function splitRows(lines: DiffFile["lines"]): SplitRow[] {
  const rows: SplitRow[] = [];
  let removed: CodeLine[] = [], added: CodeLine[] = [];
  function flush() {
    for (let i = 0; i < Math.max(removed.length, added.length); i++) {
      rows.push({ kind: "code", old: removed[i], new: added[i] });
    }
    removed = [];
    added = [];
  }
  for (const line of lines) {
    if (line.kind === "del") removed.push(line);
    else if (line.kind === "add") added.push(line);
    else {
      flush();
      if (line.kind === "hunk" || line.kind === "note") rows.push(line);
      else rows.push({ kind: "code", old: line, new: line });
    }
  }
  flush();
  return rows;
}

const PHONE = "(max-width: 760px)";
function subscribeToWidth(change: () => void) {
  const media = window.matchMedia(PHONE);
  media.addEventListener("change", change);
  return () => media.removeEventListener("change", change);
}

function Code({ line, highlighted }: { line: CodeLine; highlighted?: HighlightedLines }) {
  const tokens = highlighted?.get(line);
  const mark = line.kind === "add" ? "+" : line.kind === "del" ? "−" : " ";
  return <>
    <span className="patch-marker" aria-hidden={line.kind === "context"}>{mark}</span>
    {tokens ? tokens.map((token, index) => <span key={index} style={{ color: token.color }}>{token.content}</span>) : line.text}
  </>;
}

function DiffSide({ line, side, highlighted }: {
  line?: CodeLine; side: "old" | "new"; highlighted?: HighlightedLines;
}) {
  const tone = line?.kind ?? "empty";
  return <>
    <td className={`patch-number patch-${tone}`}>{line?.[side]}</td>
    <td className={`patch-code patch-${tone}`}>{line && <Code line={line} highlighted={highlighted} />}</td>
  </>;
}

export function DiffView({ diff }: { diff: string }) {
  const files = useMemo(() => parseDiff(diff), [diff]);
  const [syntax, setSyntax] = useState<{ files: DiffFile[]; lines: HighlightedLines }>();
  const [layout, setLayout] = useState<"unified" | "split">();
  const phone = useSyncExternalStore(subscribeToWidth, () => window.matchMedia(PHONE).matches, () => true);
  const split = layout ? layout === "split" : !phone;

  useEffect(() => {
    if (!files.length) return;
    let cancelled = false;
    void import("@/lib/diff-highlighting")
      .then(({ highlightDiff }) => highlightDiff(files))
      .then((lines) => { if (!cancelled) setSyntax({ files, lines }); })
      .catch(() => { /* The raw lines remain readable while highlighting is unavailable. */ });
    return () => { cancelled = true; };
  }, [files]);

  if (!files.length) return diff ? <pre className="analysis-result">{diff}</pre> : <p>No changes to show.</p>;
  const highlighted = syntax?.files === files ? syntax.lines : undefined;

  return <div className="patch-viewer">
    <div className="patch-layout" role="group" aria-label="Diff layout">
      <button type="button" aria-pressed={!split} onClick={() => setLayout("unified")}>Unified</button>
      <button type="button" aria-pressed={split} onClick={() => setLayout("split")}>Split</button>
    </div>
    {files.map((file, index) => <section key={index} className="patch-file">
      <header className="patch-heading">
        <code>{file.path}</code>
        <span className="patch-counts" aria-label={`${file.added} additions, ${file.removed} deletions`}>
          <span>+{file.added}</span> <span>−{file.removed}</span>
        </span>
      </header>
      {file.lines.length ? <div className="patch-scroll">
        <table className={split ? "patch-table patch-split" : "patch-table"} aria-label={`Changes to ${file.path}`}>
          <colgroup>
            <col className="patch-gutter" />
            {split && <col />}
            <col className="patch-gutter" />
            <col />
          </colgroup>
          <thead className="sr-only"><tr>
            <th scope="col">Old line</th>
            {split && <th scope="col">Original code</th>}
            <th scope="col">New line</th>
            <th scope="col">{split ? "New code" : "Code"}</th>
          </tr></thead>
          <tbody>{(split ? splitRows(file.lines) : file.lines).map((line, row) => {
            if (line.kind === "hunk" || line.kind === "note") {
              return <tr key={row} className={`patch-${line.kind}`}><td colSpan={split ? 4 : 3}>{line.text}</td></tr>;
            }
            if (line.kind === "code") return <tr key={row}>
              <DiffSide line={line.old} side="old" highlighted={highlighted} />
              <DiffSide line={line.new} side="new" highlighted={highlighted} />
            </tr>;
            return <tr key={row} className={`patch-${line.kind}`}>
              <td className="patch-number">{line.old}</td>
              <td className="patch-number">{line.new}</td>
              <td className="patch-code"><Code line={line} highlighted={highlighted} /></td>
            </tr>;
          })}</tbody>
        </table>
      </div> : <p className="patch-empty">No text lines changed.</p>}
    </section>)}
  </div>;
}
