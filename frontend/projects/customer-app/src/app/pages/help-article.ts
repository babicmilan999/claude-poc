import { Location } from '@angular/common';
import { Component, computed, inject, input } from '@angular/core';
import { humanize } from '@poc/api-client';

/**
 * Placeholder for a knowledge base article link (`Source.url`, by default `/help/<slug>`
 * from the agent service's `KB_ARTICLE_BASE_URL`). The agent API has no endpoint that
 * serves articles yet, so this page only names the article.
 */
@Component({
  selector: 'ca-help-article',
  template: `
    <section class="card">
      <h2>{{ title() }}</h2>
      <p class="muted">Help article <code>{{ slug() }}</code>. Articles are not available in the app yet; the
        assistant's answer quotes the parts that apply to you.</p>
      <button type="button" class="link" (click)="back()">Back</button>
    </section>
  `,
})
export class HelpArticlePage {
  private readonly location = inject(Location);
  readonly slug = input.required<string>();
  readonly title = computed(() => humanize(this.slug().replace(/-/g, '_')));

  back(): void {
    this.location.back();
  }
}
