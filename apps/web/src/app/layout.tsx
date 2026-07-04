import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";

// Self-hosted variable fonts (Vol 4 §3.3 — no font CDN, offline requirement).
const inter = localFont({
  src: "../fonts/inter-latin-wght-normal.woff2",
  variable: "--font-inter",
  display: "swap",
});

const jetbrainsMono = localFont({
  src: "../fonts/jetbrains-mono-latin-wght-normal.woff2",
  variable: "--font-jetbrains-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    default: "TITAN",
    template: "%s · TITAN",
  },
  description:
    "A team of specialized AI agents that plans, builds, tests, and ships software. You are the client.",
};

// ADR-FRT-06: dark is the default; a blocking inline script applies the
// persisted choice before first paint so there is never a theme flash.
// SSR emits class="dark", so anonymous first paint is dark by construction;
// the script only removes it when the user chose light.
const themeScript = `try{if(localStorage.getItem("titan.theme")==="light")document.documentElement.classList.remove("dark");}catch(e){}
try{if(localStorage.getItem("titan.sidebar")==="collapsed")document.documentElement.dataset.sidebar="collapsed";}catch(e){}`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className={`${inter.variable} ${jetbrainsMono.variable} font-sans`}>
        {children}
      </body>
    </html>
  );
}
