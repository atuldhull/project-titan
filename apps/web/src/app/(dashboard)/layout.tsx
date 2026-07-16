import { redirect } from "next/navigation";

import { getSession } from "@/lib/auth/session";
import { Providers } from "@/app/providers";
import { Sidebar } from "@/components/shell/sidebar";
import { TopBar } from "@/components/shell/topbar";

/**
 * Auth-gated app shell (Vol 4 §2.3, §11). The session is resolved server-side
 * from the httpOnly cookie and seeded into the client providers; no session →
 * redirect to login (the server is the authority, TITAN-FRT-037). The socket,
 * query cache, and toast host all mount here, inside the gate.
 */
export default async function DashboardLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const session = await getSession();
  if (!session) {
    redirect("/login");
  }
  return (
    <Providers session={session}>
      <div className="flex h-dvh">
        <Sidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          <TopBar />
          <main className="flex-1 overflow-y-auto p-4 md:p-6">{children}</main>
        </div>
      </div>
    </Providers>
  );
}
