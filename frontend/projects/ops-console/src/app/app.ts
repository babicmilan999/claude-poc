import { Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

@Component({
  selector: 'ops-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  template: `
    <header class="top">
      <span class="brand">Ops copilot</span>
      <nav>
        <a routerLink="/cases" routerLinkActive="active" [routerLinkActiveOptions]="{ exact: true }">Case queue</a>
        <a routerLink="/cases/new" routerLinkActive="active">New case</a>
        <a routerLink="/conversations" routerLinkActive="active">Conversations</a>
      </nav>
    </header>
    <main class="page">
      <router-outlet />
    </main>
  `,
})
export class App {}
