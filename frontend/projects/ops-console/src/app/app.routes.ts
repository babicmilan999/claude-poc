import { Routes } from '@angular/router';
import { CaseDetailPage } from './pages/case-detail';
import { CaseQueuePage } from './pages/case-queue';
import { ConversationPage } from './pages/conversation';
import { ConversationsPage } from './pages/conversations';
import { NewCasePage } from './pages/new-case';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'cases' },
  { path: 'cases', component: CaseQueuePage, title: 'Case queue' },
  { path: 'cases/new', component: NewCasePage, title: 'New case' },
  { path: 'cases/:id', component: CaseDetailPage, title: 'Case' },
  { path: 'conversations', component: ConversationsPage, title: 'Conversations' },
  { path: 'conversations/:id', component: ConversationPage, title: 'Conversation' },
  { path: '**', redirectTo: 'cases' },
];
