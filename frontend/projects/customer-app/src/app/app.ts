import { Component, inject } from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { CustomerSession } from './session';

@Component({
  selector: 'ca-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  template: `
    <div class="phone">
      <header class="top">
        <span class="brand">Bank</span>
        @if (session.customer(); as customer) {
          <span class="who">{{ customer.name }}</span>
          <button type="button" class="link" (click)="logOut()">Log out</button>
        } @else {
          <span class="who muted">Not logged in</span>
        }
      </header>
      <main class="screen">
        <router-outlet />
      </main>
      <nav class="tabs">
        @if (session.customer()) {
          <a routerLink="/account" routerLinkActive="active">Account</a>
          <a routerLink="/chat" routerLinkActive="active">Help</a>
        } @else {
          <a routerLink="/login" routerLinkActive="active">Log in as</a>
        }
        <a routerLink="/watch" routerLinkActive="active">Watch</a>
      </nav>
    </div>
  `,
})
export class App {
  protected readonly session = inject(CustomerSession);
  private readonly router = inject(Router);

  logOut(): void {
    this.session.logOut();
    this.router.navigateByUrl('/login');
  }
}
