import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import "./index.css";
import { AppShell } from "@/components/app-shell";
import { DashboardPage } from "@/pages/dashboard";
import { ChatPage } from "@/pages/chat";
import { SourcesPage } from "@/pages/sources";
import { SettingsPage } from "@/pages/settings";

const router = createBrowserRouter([
  {
    element: <AppShell />,
    children: [
      { path: "/", element: <DashboardPage /> },
      { path: "/chat", element: <ChatPage /> },
      { path: "/sources", element: <SourcesPage /> },
      { path: "/settings", element: <SettingsPage /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>
);
