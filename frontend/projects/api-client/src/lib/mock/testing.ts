/** Small LCG so mock ids and picks are repeatable in tests. */
export function seededRandom(seed = 42): () => number {
  let s = seed;
  return () => (s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296;
}
