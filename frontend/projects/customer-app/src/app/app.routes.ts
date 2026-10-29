import { inject } from '@angular/core';
import { CanActivateFn, Router, Routes } from '@angular/router';
import { AccountPage } from './pages/account';
import { ChatPage } from './pages/chat';
import { HelpArticlePage } from './pages/help-article';
import { LoginPage } from './pages/login';
import { WatchListPage } from './pages/watch-list';
import { WatchPage } from './pages/watch';
import { CustomerSession } from './session';

const loggedIn: CanActivateFn = () =>
  inject(CustomerSession).customer() ? true : inject(Router).parseUrl('/login');

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'login' },
  { path: 'login', component: LoginPage, title: 'Log in as' },
  { path: 'account', component: AccountPage, canActivate: [loggedIn], title: 'Account' },
  { path: 'chat', component: ChatPage, canActivate: [loggedIn], title: 'Help' },
  { path: 'help/:slug', component: HelpArticlePage, title: 'Help article' },
  { path: 'watch', component: WatchListPage, title: 'Watch mode' },
  { path: 'watch/:id', component: WatchPage, title: 'Watch mode' },
  { path: '**', redirectTo: 'login' },
];
