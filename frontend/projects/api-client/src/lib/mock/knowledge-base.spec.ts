// Every help article the mock cites must exist in knowledge-base/ (slug = file name without
// .md), and its title must match the article's first heading, so the mock never invents
// sources the real assistant could not cite.

import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Source } from '../models';
import { GENERIC_SOURCES, SCRIPTS } from './scenarios';

const article = (slug: string) => fileURLToPath(new URL(`../../../../../../knowledge-base/${slug}.md`, import.meta.url));

const cited: Source[] = [...SCRIPTS.flatMap((s) => s.sources), ...GENERIC_SOURCES].filter((s) => s.kind === 'article');

describe('mock article sources vs knowledge-base/', () => {
  it('cites at least one article', () => {
    expect(cited.length).toBeGreaterThan(0);
  });

  for (const source of cited) {
    it(`${source.article_id} exists and its title matches the heading`, () => {
      expect(source.article_id).toBeTruthy();
      const path = article(source.article_id!);
      expect(existsSync(path), `missing knowledge-base/${source.article_id}.md`).toBe(true);
      const heading = readFileSync(path, 'utf8')
        .split('\n')
        .find((line) => line.startsWith('# '));
      expect(heading?.slice(2).trim()).toBe(source.title);
    });
  }
});
