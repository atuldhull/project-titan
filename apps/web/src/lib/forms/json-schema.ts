/**
 * JSON Schema → form model → zod (Vol 4 §6.12, TITAN-FRT-035).
 *
 * The requirement is that a plugin's config form be GENERATED from its manifest
 * schema, so that adding a plugin never means adding a form. Two facts about
 * this codebase shape the design:
 *
 * 1. The app already has ONE form engine — `useZodForm` (react-hook-form + a zod
 *    resolver) — and §6.13 requires inline per-field zod messages. So rather
 *    than build a second, parallel validation path, this compiles the JSON
 *    Schema *into* a zod schema and hands it to the engine that already exists.
 * 2. Plugin manifests are author-supplied and unvalidated at this layer, so
 *    every function here treats its input as hostile: unknown types degrade to
 *    a rendered-but-inert field rather than throwing during render.
 *
 * Deliberately supports a SUBSET — flat objects of string/number/integer/
 * boolean/enum/string-array. That is what a connector's settings actually look
 * like (see `plugins/first_party/sample/config.schema.json`), and a field this
 * cannot render is reported through {@link unsupportedFields} rather than
 * silently dropped, because dropping one means saving a config that omits it.
 */
import { z } from "zod";

/**
 * What a saved secret reads as. MUST equal
 * `titan.plugins.manager.SECRET_SENTINEL` — the server masks with it and
 * recognises it on the way back as "leave this one alone", so a drift between
 * the two either destroys a working credential or stores four bullets as one.
 */
export const SECRET_SENTINEL = "••••";

/** JSON Schema, as far as we trust it: an object of unknown shape. */
export type JsonSchema = Readonly<Record<string, unknown>>;

export type FieldKind =
  | "string"
  | "textarea"
  | "number"
  | "integer"
  | "boolean"
  | "enum"
  | "string-array"
  | "secret";

export interface SchemaField {
  readonly name: string;
  readonly kind: FieldKind;
  readonly label: string;
  readonly description?: string;
  readonly required: boolean;
  /** Present for `enum`. */
  readonly options?: readonly string[];
  readonly minLength?: number;
  readonly maxLength?: number;
  readonly minimum?: number;
  readonly maximum?: number;
  readonly defaultValue?: unknown;
}

export interface FormModel {
  readonly title?: string;
  readonly description?: string;
  readonly fields: readonly SchemaField[];
  /** Property names this renderer cannot represent, with the reason. Surfaced
   *  in the UI: a silently omitted field is a config saved without it. */
  readonly unsupported: readonly { name: string; reason: string }[];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : undefined;
}

/** A human label: the schema's own `title`, else the property name unsnaked. */
function labelOf(name: string, spec: Record<string, unknown>): string {
  const title = asString(spec.title);
  if (title) return title;
  const words = name.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Is this property a secret?
 *
 * `writeOnly` is JSON Schema's own vocabulary for "never present in a response"
 * (draft 2019-09 §9.4), and the backend's `secret_keys_of` uses exactly the same
 * two rules — the two MUST agree, because the server masks based on its answer
 * and the client decides whether to submit the mask back based on this one. If
 * they disagreed, a field the server masked but the client treated as ordinary
 * would send `••••` back as the real value.
 */
export function isSecretField(spec: Record<string, unknown>): boolean {
  return spec.writeOnly === true || spec.format === "password";
}

function kindOf(spec: Record<string, unknown>): FieldKind | null {
  if (isSecretField(spec)) return "secret";
  if (Array.isArray(spec.enum)) return "enum";

  const type = asString(spec.type);
  switch (type) {
    case "string":
      // A long free-text field gets a textarea; the threshold is arbitrary but
      // the alternative — a single-line input for a 4000-char field — is worse.
      return (asNumber(spec.maxLength) ?? 0) > 512 ? "textarea" : "string";
    case "integer":
      return "integer";
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    case "array": {
      const items = asRecord(spec.items);
      return items && asString(items.type) === "string" ? "string-array" : null;
    }
    default:
      return null;
  }
}

/** Parse a JSON Schema into a renderable field list. */
export function toFormModel(schema: JsonSchema | null | undefined): FormModel {
  const root = asRecord(schema);
  const properties = root ? asRecord(root.properties) : null;
  if (!root || !properties) {
    return { fields: [], unsupported: [] };
  }
  const required = new Set(
    Array.isArray(root.required)
      ? root.required.filter((r): r is string => typeof r === "string")
      : [],
  );

  const fields: SchemaField[] = [];
  const unsupported: { name: string; reason: string }[] = [];

  for (const [name, raw] of Object.entries(properties)) {
    const spec = asRecord(raw);
    if (!spec) {
      unsupported.push({ name, reason: "not a schema object" });
      continue;
    }
    const kind = kindOf(spec);
    if (kind === null) {
      unsupported.push({
        name,
        reason: `unsupported type '${asString(spec.type) ?? "unknown"}'`,
      });
      continue;
    }
    const options = Array.isArray(spec.enum)
      ? spec.enum.filter((v): v is string => typeof v === "string")
      : undefined;
    if (kind === "enum" && (!options || options.length === 0)) {
      unsupported.push({ name, reason: "enum with no string values" });
      continue;
    }
    fields.push({
      name,
      kind,
      label: labelOf(name, spec),
      description: asString(spec.description),
      required: required.has(name),
      options,
      minLength: asNumber(spec.minLength),
      maxLength: asNumber(spec.maxLength),
      minimum: asNumber(spec.minimum),
      maximum: asNumber(spec.maximum),
      defaultValue: spec.default,
    });
  }

  return {
    title: asString(root.title),
    description: asString(root.description),
    fields,
    unsupported,
  };
}

export function unsupportedFields(model: FormModel): readonly string[] {
  return model.unsupported.map((u) => u.name);
}

/** The secret field names, in the order they render. */
export function secretFieldNames(model: FormModel): readonly string[] {
  return model.fields.filter((f) => f.kind === "secret").map((f) => f.name);
}

function stringRule(field: SchemaField): z.ZodTypeAny {
  let rule = z.string();
  if (field.minLength !== undefined) {
    rule = rule.min(
      field.minLength,
      `Must be at least ${field.minLength} characters.`,
    );
  }
  if (field.maxLength !== undefined) {
    rule = rule.max(
      field.maxLength,
      `Must be at most ${field.maxLength} characters.`,
    );
  }
  if (field.required) {
    return rule.min(1, `${field.label} is required.`);
  }
  // Optional strings: an empty input is "not set", not a validation failure.
  return rule.optional().or(z.literal(""));
}

function numberRule(field: SchemaField): z.ZodTypeAny {
  let rule = field.kind === "integer" ? z.number().int() : z.number();
  if (field.minimum !== undefined) {
    rule = rule.min(field.minimum, `Must be at least ${field.minimum}.`);
  }
  if (field.maximum !== undefined) {
    rule = rule.max(field.maximum, `Must be at most ${field.maximum}.`);
  }
  // The DOM gives us strings. Coerce here rather than at every call site, and
  // reject a non-numeric string instead of silently producing NaN — which would
  // serialize to `null` and wipe the saved value.
  return z.preprocess(
    (value) =>
      value === "" || value === undefined ? undefined : Number(value),
    field.required ? rule : rule.optional(),
  );
}

/** Compile a form model into a zod object schema for {@link useZodForm}. */
export function toZodSchema(model: FormModel): z.ZodType<FormValues> {
  const shape: Record<string, z.ZodTypeAny> = {};
  for (const field of model.fields) {
    switch (field.kind) {
      case "boolean":
        shape[field.name] = z.boolean().optional();
        break;
      case "number":
      case "integer":
        shape[field.name] = numberRule(field);
        break;
      case "enum":
        shape[field.name] = field.required
          ? z.string().min(1, `${field.label} is required.`)
          : z.string().optional().or(z.literal(""));
        break;
      case "string-array":
        shape[field.name] = z.array(z.string()).optional();
        break;
      case "secret":
        // The mask must satisfy the rule unconditionally. A saved secret comes
        // back as `••••` (4 characters), so a schema declaring `minLength: 8`
        // — as the shipped sample plugin does — would fail validation on a
        // form the operator never touched, making the whole dialog
        // unsubmittable until they retyped the real credential. That is
        // precisely what the mask exists to avoid. Constraints still apply to
        // anything the operator actually types.
        shape[field.name] = z.union([
          z.literal(SECRET_SENTINEL),
          stringRule(field),
        ]);
        break;
      default:
        shape[field.name] = stringRule(field);
    }
  }
  // `strip`, not `strict`: a saved config may contain keys the CURRENT schema no
  // longer declares (the plugin was upgraded). Rejecting the whole form because
  // of a stale key would make the plugin unconfigurable until someone edited the
  // database by hand.
  return z.object(shape) as unknown as z.ZodType<FormValues>;
}

export type FormValues = Record<string, unknown>;

/**
 * Initial form values: saved value, else schema default, else an empty of the
 * right type.
 *
 * Every field gets an entry. react-hook-form treats an absent key as an
 * uncontrolled input, and an input that flips from uncontrolled to controlled
 * when data arrives loses whatever was typed in between.
 */
export function initialValues(
  model: FormModel,
  saved: Readonly<Record<string, unknown>> = {},
): FormValues {
  const values: FormValues = {};
  for (const field of model.fields) {
    const current = saved[field.name];
    if (current !== undefined) {
      values[field.name] = current;
      continue;
    }
    if (field.defaultValue !== undefined) {
      values[field.name] = field.defaultValue;
      continue;
    }
    switch (field.kind) {
      case "boolean":
        values[field.name] = false;
        break;
      case "string-array":
        values[field.name] = [];
        break;
      case "number":
      case "integer":
        values[field.name] = "";
        break;
      default:
        values[field.name] = "";
    }
  }
  return values;
}

/**
 * Strip values that mean "unset" before submitting.
 *
 * An empty optional string is absence, not the empty string: persisting `""`
 * for an unset optional field makes `additionalProperties: false` schemas fail
 * server-side validation and makes "is it configured?" unanswerable.
 *
 * Secrets are NOT stripped here — an unchanged secret field still holds the
 * mask, and the SERVER is what turns the mask back into the stored value. The
 * client must send it so the server can recognise "unchanged"; dropping it here
 * would read as "clear this secret".
 */
export function toSubmitPayload(
  model: FormModel,
  values: FormValues,
  saved: Readonly<Record<string, unknown>> = {},
): Record<string, unknown> {
  // Start from the keys this form could NOT represent, so they survive.
  // `configure` REPLACES the stored config, so anything omitted here is
  // deleted — which would silently destroy a field the schema no longer
  // renders (a `type: object` block, a numeric array) and, worse, the
  // supervisor's own `risk_overrides` key, which no schema declares at all.
  // The panel promises these are preserved; this is what makes that true.
  const rendered = new Set(model.fields.map((f) => f.name));
  const payload: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(saved)) {
    if (!rendered.has(key)) payload[key] = value;
  }
  for (const field of model.fields) {
    const value = values[field.name];
    if (value === undefined || value === null) continue;
    if (typeof value === "string" && value === "" && !field.required) continue;
    if (Array.isArray(value) && value.length === 0 && !field.required) continue;
    payload[field.name] =
      (field.kind === "number" || field.kind === "integer") &&
      typeof value === "string"
        ? Number(value)
        : value;
  }
  return payload;
}
