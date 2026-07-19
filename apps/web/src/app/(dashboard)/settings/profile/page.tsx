import type { Metadata } from "next";

import { ProfileScreen } from "@/components/settings/profile-screen";

export const metadata: Metadata = { title: "Profile" };

export default function Page() {
  return <ProfileScreen />;
}
