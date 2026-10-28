import { ComponentFixture, TestBed } from '@angular/core/testing';
import { MockAgentServer } from '@poc/api-client';

/** A fresh mock agent service with no delays, for one test. */
export function mockServer(): MockAgentServer {
  return new MockAgentServer({ tickMs: 0 });
}

/**
 * Re-renders until `predicate` holds. The mock answers through promises and streams that
 * Angular does not track, so `whenStable()` alone is not enough.
 */
export async function waitFor(fixture: ComponentFixture<unknown>, predicate: () => boolean, timeoutMs = 3000) {
  const start = Date.now();
  for (;;) {
    fixture.detectChanges();
    await fixture.whenStable();
    if (predicate()) return;
    if (Date.now() - start > timeoutMs) throw new Error('waitFor: condition not met in time');
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

export function text(fixture: ComponentFixture<unknown>): string {
  return (fixture.nativeElement as HTMLElement).textContent ?? '';
}

export function el<T extends Element = HTMLElement>(fixture: ComponentFixture<unknown>, selector: string): T {
  const found = (fixture.nativeElement as HTMLElement).querySelector<T>(selector);
  if (!found) throw new Error(`no element ${selector}`);
  return found;
}

export function all<T extends Element = HTMLElement>(fixture: ComponentFixture<unknown>, selector: string): T[] {
  return [...(fixture.nativeElement as HTMLElement).querySelectorAll<T>(selector)];
}

export function typeInto(input: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  input.value = value;
  input.dispatchEvent(new Event('input'));
}

export { TestBed };
