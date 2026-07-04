import {
  Blocks,
  Bot,
  Brain,
  Building2,
  FolderKanban,
  GitBranch,
  Landmark,
  LayoutGrid,
  LineChart,
  MessagesSquare,
  Rocket,
  ScrollText,
  Settings,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  title: string;
  href: string;
  icon: LucideIcon;
}

/** Organization-scoped surfaces (Vol 4 §2.1/§2.2). */
export const orgNavItems: NavItem[] = [
  { title: "Org", href: "/org", icon: Building2 },
  { title: "Projects", href: "/projects", icon: FolderKanban },
  { title: "Agents", href: "/agents", icon: Bot },
  { title: "Conversations", href: "/conversations", icon: MessagesSquare },
  { title: "Logs", href: "/logs", icon: ScrollText },
  { title: "Deployments", href: "/deployments", icon: Rocket },
  { title: "Monitoring", href: "/monitoring", icon: LineChart },
  { title: "Memory", href: "/memory", icon: Brain },
  { title: "Plugins", href: "/plugins", icon: Blocks },
  { title: "Settings", href: "/settings/profile", icon: Settings },
];

/** Project-scoped surfaces, shown when a project is in the URL (TITAN-FRT-006). */
export function projectNavItems(pid: string): NavItem[] {
  return [
    { title: "Overview", href: `/projects/${pid}`, icon: LayoutGrid },
    { title: "Board", href: `/projects/${pid}/board`, icon: FolderKanban },
    { title: "Graph", href: `/projects/${pid}/graph`, icon: GitBranch },
    {
      title: "Architecture",
      href: `/projects/${pid}/architecture`,
      icon: Landmark,
    },
    {
      title: "Deployments",
      href: `/projects/${pid}/deployments`,
      icon: Rocket,
    },
  ];
}
