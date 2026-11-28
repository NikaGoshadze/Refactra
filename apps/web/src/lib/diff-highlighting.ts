import { createHighlighterCore, type ThemedToken } from "@shikijs/core";
import { createJavaScriptRegexEngine } from "@shikijs/engine-javascript";
import type { CodeLine, DiffFile } from "@/components/diff-view";

const theme = "github-dark";
const grammars = {
  python: () => import("@shikijs/langs/python"),
  javascript: () => import("@shikijs/langs/javascript"),
  typescript: () => import("@shikijs/langs/typescript"),
  jsx: () => import("@shikijs/langs/jsx"),
  tsx: () => import("@shikijs/langs/tsx"),
  json: () => import("@shikijs/langs/json"),
  yaml: () => import("@shikijs/langs/yaml"),
};
type Language = keyof typeof grammars;
const extensions: Record<string, Language> = {
  py: "python", pyi: "python", js: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript", jsx: "jsx", tsx: "tsx", json: "json", yaml: "yaml", yml: "yaml",
};
export type HighlightedLines = Map<CodeLine, ThemedToken[]>;

let highlighter: ReturnType<typeof createHighlighterCore> | undefined;
const loading = new Map<Language, Promise<void>>();

export async function highlightDiff(files: DiffFile[]): Promise<HighlightedLines> {
  const output: HighlightedLines = new Map();
  // Bound the work across all files. The viewer always renders the complete diff as text.
  let remaining = 32_000;
  for (const file of files) {
    const extension = file.path.split(".").pop()?.toLowerCase() ?? "";
    if (!Object.hasOwn(extensions, extension) || file.lines.some((line) => line.text.length > 2000)) continue;
    const language = extensions[extension];
    const length = file.lines.reduce((sum, line) => sum + line.text.length + 1, 0);
    if (length > remaining) continue;
    remaining -= length;

    highlighter ??= createHighlighterCore({
      themes: [import("@shikijs/themes/github-dark")],
      langs: [],
      engine: createJavaScriptRegexEngine(),
    });
    const shiki = await highlighter;
    if (!loading.has(language)) loading.set(language, shiki.loadLanguage(grammars[language]()));
    await loading.get(language);

    let old: CodeLine[] = [], next: CodeLine[] = [];
    function tokenize(lines: CodeLine[]) {
      if (!lines.length) return;
      const tokens = shiki.codeToTokensBase(lines.map((line) => line.text).join("\n"), { lang: language, theme });
      lines.forEach((line, index) => { if (tokens[index]) output.set(line, tokens[index]); });
    }
    function flush() {
      tokenize(old);
      tokenize(next);
      old = [];
      next = [];
    }
    for (const line of file.lines) {
      if (line.kind === "hunk") flush();
      else if (line.kind !== "note") {
        if (line.kind !== "add") old.push(line);
        if (line.kind !== "del") next.push(line);
      }
    }
    flush();
  }
  return output;
}
