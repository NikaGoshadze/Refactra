/** Poll at `base` while responses change, then back off by 1.5× per unchanged response. */
export function pollDelay(base: number, unchanged: number, max = 15_000) {
  return Math.min(Math.round(base * 1.5 ** unchanged), max);
}

/** Track how many consecutive polls returned the same response. */
export function nextUnchanged(previous: string | null, current: string, unchanged: number) {
  return previous === current ? unchanged + 1 : 0;
}
