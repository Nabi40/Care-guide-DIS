import type { Metadata } from "next";
import type { ReactNode } from "react";
import { AppProvider } from "@/components/AppContext";
import { AppShell } from "@/components/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Document Intelligence",
  description: "Invoice extraction, validation and search",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AppProvider>
          <AppShell>{children}</AppShell>
        </AppProvider>
      </body>
    </html>
  );
}
