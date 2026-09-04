import { Navigate, Route, Routes } from 'react-router-dom';
import AppShell from './components/AppShell.jsx';
import Toasts from './components/Toasts.jsx';
import {
  ConfigView,
  DatasetsView,
  HomeView,
  ParsingView,
  QAFlowView,
  RegistryView,
  SettingsView
} from './views/CoreViews.jsx';
import ChatView from './views/ChatView.jsx';
import AssistantsView from './views/AssistantsView.jsx';
import IndexingView from './views/IndexingView.jsx';

export default function App() {
  return <>
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/home" replace />} />
        <Route path="/home" element={<HomeView />} />
        <Route path="/knowledge/registry" element={<RegistryView />} />
        <Route path="/knowledge/datasets" element={<DatasetsView />} />
        <Route path="/knowledge/parsing" element={<ParsingView />} />
        <Route path="/knowledge/index" element={<IndexingView />} />
        <Route path="/knowledge/slices" element={<IndexingView />} />
        <Route path="/knowledge/config" element={<ConfigView />} />
        <Route path="/apps/chat" element={<ChatView />} />
        <Route path="/apps/assistants" element={<AssistantsView />} />
        <Route path="/qaflow/:stage?" element={<QAFlowView />} />
        <Route path="/settings/:tab" element={<SettingsView />} />
        <Route path="*" element={<Navigate to="/home" replace />} />
      </Route>
    </Routes>
    <Toasts />
  </>;
}
