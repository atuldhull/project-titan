import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { ConfirmDeployDialog } from "./confirm-dialog";
import { evaluateGate } from "@/lib/deployments/gate";

/**
 * The FRT-032 confirmation dialog in its two shapes — the load-bearing visual
 * of §6.9. The gate policy is computed from the real `evaluateGate`, so what
 * these show (whether a typed name is demanded) is the actual rule, not a mock.
 */
const allow = () => true;

function Demo({ environment }: { environment: "local" | "prod" }) {
  const gate = evaluateGate("promote", environment, allow);
  return (
    <ConfirmDeployDialog
      open
      policy={gate}
      environment={environment}
      description={`Promote to ${environment}. This changes what is running.`}
      onCancel={() => {}}
      onConfirm={() => {}}
    />
  );
}

const meta: Meta<typeof Demo> = {
  title: "Deployments/ConfirmDialog",
  component: Demo,
};
export default meta;

type Story = StoryObj<typeof Demo>;

/** An unprotected environment: confirm restates the scope, no typed name. */
export const Unprotected: Story = { args: { environment: "local" } };

/** prod: the operator must type "prod", and Confirm stays disabled until they do. */
export const ProtectedRequiresTypedName: Story = {
  args: { environment: "prod" },
};
