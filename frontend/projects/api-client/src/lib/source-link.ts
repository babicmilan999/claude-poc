import { Source } from './models';

/**
 * How to show a `Source`:
 * - `external`: an absolute `http(s)` URL, opened in a new tab;
 * - `internal`: a path on the app's own origin, e.g. `/help/why-cards-get-declined`
 *   (the agent service's default `KB_ARTICLE_BASE_URL` is `/help/`), routed inside the app;
 * - `none`: a chip without a link (account data, or an article with no usable URL).
 */
export type SourceLink =
  | { kind: 'external'; href: string }
  | { kind: 'internal'; path: string }
  | { kind: 'none' };

export function sourceLink(source: Source): SourceLink {
  if (source.kind !== 'article') return { kind: 'none' };
  const url = source.url?.trim();
  if (url) {
    if (/^https?:\/\//i.test(url)) return { kind: 'external', href: url };
    // Same-origin path; `//host` is protocol-relative (external), not a path.
    if (url.startsWith('/') && !url.startsWith('//')) return { kind: 'internal', path: url };
  }
  if (source.article_id) return { kind: 'internal', path: `/help/${encodeURIComponent(source.article_id)}` };
  return { kind: 'none' };
}
