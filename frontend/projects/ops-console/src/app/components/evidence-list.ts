import { JsonPipe } from '@angular/common';
import { Component, input } from '@angular/core';
import { Evidence } from '@poc/api-client';

/** Evidence items (tool results), each anchored by id so a diagnosis can link to it. */
@Component({
  selector: 'ops-evidence-list',
  imports: [JsonPipe],
  template: `
    @for (e of evidence(); track e.id) {
      <div class="ev" [id]="anchor(e.id)" [class.hl]="highlighted() === e.id">
        <span class="badge">{{ e.id }}</span>
        <code>{{ e.tool }}</code>
        <span>{{ e.summary }}</span>
        @if (e.data !== undefined) {
          <details><summary class="muted">data</summary><pre>{{ e.data | json }}</pre></details>
        }
      </div>
    } @empty {
      <p class="muted">No evidence.</p>
    }
  `,
  styles: `
    .ev { padding: 6px 8px; border-radius: 6px; display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
    .ev.hl { background: #fff6d6; }
    details { flex-basis: 100%; }
  `,
})
export class EvidenceList {
  readonly evidence = input.required<Evidence[]>();
  readonly prefix = input('ev');
  readonly highlighted = input<string | null>(null);

  anchor(id: string): string {
    return `${this.prefix()}-${id}`;
  }
}
