import { redirect } from "next/navigation";

// Vol 4 §2.2: / redirects to /org.
export default function Home() {
  redirect("/org");
}
