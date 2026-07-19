"use client";

import { useEffect, useMemo, useState } from "react";
import type { UseFormReturn } from "react-hook-form";

import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  toFormModel,
  toZodSchema,
  type FormModel,
  type FormValues,
  type JsonSchema,
  type SchemaField,
} from "@/lib/forms/json-schema";
import { useZodForm } from "@/lib/forms/use-zod-form";

/**
 * The generated plugin config form (TITAN-FRT-035).
 *
 * ONE renderer for every plugin: fields come from the manifest's JSON Schema,
 * so installing a plugin never means writing a form. Validation is the app's
 * existing zod + react-hook-form engine — the schema is compiled to zod rather
 * than validated by a second, parallel mechanism, which is what gets §6.13's
 * inline per-field messages for free.
 *
 * Secret fields render as password inputs pre-filled with the server's mask.
 * The mask is submitted back unchanged when untouched, and the SERVER is what
 * turns it back into the stored value — the client never holds the secret, so
 * it could not restore one itself.
 */
export function useSchemaForm(
  schema: JsonSchema | null | undefined,
  values: Readonly<Record<string, unknown>>,
): { model: FormModel; form: UseFormReturn<FormValues> } {
  const model = useMemo(() => toFormModel(schema), [schema]);
  const zodSchema = useMemo(() => toZodSchema(model), [model]);
  const form = useZodForm(zodSchema, { defaultValues: values });
  return { model, form };
}

export function SchemaFields({
  model,
  form,
  disabled = false,
}: {
  readonly model: FormModel;
  readonly form: UseFormReturn<FormValues>;
  readonly disabled?: boolean;
}) {
  if (model.fields.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        This plugin declares no configurable settings.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {model.fields.map((field) => (
        <SchemaFieldRow
          key={field.name}
          field={field}
          form={form}
          disabled={disabled}
        />
      ))}

      {model.unsupported.length > 0 ? (
        // NEVER silently drop a field: saving a form that omitted one would
        // write a config missing a setting the plugin needs, and the operator
        // would have no way to know.
        <div
          role="status"
          className="rounded-md border border-dashed p-3 text-xs text-muted-foreground"
        >
          <p className="font-medium text-foreground">
            Not editable here ({model.unsupported.length})
          </p>
          <ul className="mt-1 space-y-0.5">
            {model.unsupported.map((entry) => (
              <li key={entry.name}>
                <span className="font-mono">{entry.name}</span> — {entry.reason}
              </li>
            ))}
          </ul>
          <p className="mt-1">
            Existing values for these are preserved on save.
          </p>
        </div>
      ) : null}
    </div>
  );
}

function SchemaFieldRow({
  field,
  form,
  disabled,
}: {
  readonly field: SchemaField;
  readonly form: UseFormReturn<FormValues>;
  readonly disabled: boolean;
}) {
  const id = `plugin-config-${field.name}`;
  const error = form.formState.errors[field.name];
  const message = typeof error?.message === "string" ? error.message : null;
  const describedBy = field.description ? `${id}-description` : undefined;

  return (
    <div className="space-y-2">
      <Label htmlFor={id}>
        {field.label}
        {field.required ? (
          <span aria-hidden className="text-destructive">
            {" "}
            *
          </span>
        ) : null}
      </Label>

      {renderControl(field, form, id, disabled, describedBy)}

      {field.description ? (
        <p id={describedBy} className="text-xs text-muted-foreground">
          {field.description}
        </p>
      ) : null}
      {message ? (
        <p role="alert" className="text-xs text-destructive">
          {message}
        </p>
      ) : null}
    </div>
  );
}

function renderControl(
  field: SchemaField,
  form: UseFormReturn<FormValues>,
  id: string,
  disabled: boolean,
  describedBy: string | undefined,
) {
  const invalid = Boolean(form.formState.errors[field.name]);
  const common = {
    id,
    disabled,
    "aria-invalid": invalid,
    "aria-describedby": describedBy,
  };

  switch (field.kind) {
    case "boolean": {
      const value = form.watch(field.name);
      return (
        <div className="flex items-center gap-2">
          <Checkbox
            id={id}
            disabled={disabled}
            checked={value === true}
            onCheckedChange={(next) =>
              form.setValue(field.name, next === true, { shouldDirty: true })
            }
          />
          <span className="text-sm text-muted-foreground">
            {value === true ? "Enabled" : "Disabled"}
          </span>
        </div>
      );
    }
    case "enum": {
      const value = form.watch(field.name);
      return (
        <Select
          disabled={disabled}
          value={typeof value === "string" ? value : ""}
          onValueChange={(next) =>
            form.setValue(field.name, next, { shouldDirty: true })
          }
        >
          <SelectTrigger id={id} aria-invalid={invalid} className="max-w-md">
            <SelectValue placeholder="Choose…" />
          </SelectTrigger>
          <SelectContent>
            {(field.options ?? []).map((option) => (
              <SelectItem key={option} value={option}>
                {option}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    }
    case "secret":
      return (
        <Input
          {...common}
          type="password"
          autoComplete="new-password"
          {...form.register(field.name)}
        />
      );
    case "textarea":
      return <Textarea {...common} rows={4} {...form.register(field.name)} />;
    case "number":
    case "integer":
      return (
        <Input
          {...common}
          type="number"
          step={field.kind === "integer" ? 1 : "any"}
          min={field.minimum}
          max={field.maximum}
          className="max-w-xs"
          {...form.register(field.name)}
        />
      );
    case "string-array":
      // A comma-separated line. It has to keep its own RAW text: deriving the
      // displayed value from the parsed array round-trips through
      // textToArray→arrayToText on every keystroke, and that pair is lossy —
      // it trims and drops empties, so the moment you type "a," the value
      // becomes ["a"] and re-renders as "a", deleting the comma you just
      // typed. A multi-value list was literally unenterable.
      return <StringArrayInput field={field} form={form} common={common} />;
    default:
      return <Input {...common} {...form.register(field.name)} />;
  }
}

/**
 * A comma-separated editor that owns its own text.
 *
 * The array is the stored value; the text is what the operator is typing. They
 * are only reconciled in one direction (text → array), so an in-progress
 * "a, b," survives a re-render instead of being normalised back to "a, b".
 */
function StringArrayInput({
  field,
  form,
  common,
}: {
  readonly field: SchemaField;
  readonly form: UseFormReturn<FormValues>;
  readonly common: Record<string, unknown>;
}) {
  const stored = form.watch(field.name);
  const [text, setText] = useState(() => arrayToText(stored));

  // Re-seed only when the FORM was reset (a different plugin's config landed),
  // never on our own edits — comparing against the projection of our own text
  // is what distinguishes the two.
  useEffect(() => {
    const incoming = arrayToText(stored);
    setText((current) =>
      arrayToText(textToArray(current)) === incoming ? current : incoming,
    );
  }, [stored]);

  return (
    <Input
      {...common}
      placeholder="comma, separated, values"
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        form.setValue(field.name, textToArray(event.target.value), {
          shouldDirty: true,
        });
      }}
    />
  );
}

export function arrayToText(value: unknown): string {
  return Array.isArray(value) ? value.map(String).join(", ") : "";
}

export function textToArray(text: string): string[] {
  return text
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}
