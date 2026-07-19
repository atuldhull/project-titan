/**
 * JSON Schema → form → zod (TITAN-FRT-035).
 *
 * The requirement is that a plugin's config form be generated, so the failure
 * mode that matters is a field the renderer cannot represent being silently
 * dropped: saving then writes a config missing a setting the plugin needs, and
 * nothing tells the operator.
 */
import { describe, expect, it } from "vitest";

import {
  initialValues,
  SECRET_SENTINEL,
  isSecretField,
  secretFieldNames,
  toFormModel,
  toSubmitPayload,
  toZodSchema,
  unsupportedFields,
} from "@/lib/forms/json-schema";

const SCHEMA = {
  title: "Sample settings",
  type: "object",
  properties: {
    workspace: { type: "string", title: "Workspace", minLength: 1 },
    // minLength MATTERS here, and the shipped sample plugin declares one. An
    // earlier version of this fixture omitted it, which is exactly why the
    // suite stayed green while the 4-character mask failed the rule and made
    // the real plugin's config form permanently unsubmittable.
    api_token: { type: "string", writeOnly: true, minLength: 8 },
    legacy: { type: "string", format: "password" },
    interval: { type: "integer", minimum: 1, maximum: 60, default: 15 },
    environment: { type: "string", enum: ["production", "staging"] },
    verify_tls: { type: "boolean", default: true },
    labels: { type: "array", items: { type: "string" } },
  },
  required: ["workspace"],
};

describe("toFormModel", () => {
  it("reads a field per property, in schema order", () => {
    const model = toFormModel(SCHEMA);
    expect(model.fields.map((f) => f.name)).toEqual([
      "workspace",
      "api_token",
      "legacy",
      "interval",
      "environment",
      "verify_tls",
      "labels",
    ]);
    expect(model.title).toBe("Sample settings");
  });

  it("maps each JSON Schema type to a control kind", () => {
    const byName = Object.fromEntries(
      toFormModel(SCHEMA).fields.map((f) => [f.name, f.kind]),
    );
    expect(byName).toEqual({
      workspace: "string",
      api_token: "secret",
      legacy: "secret",
      interval: "integer",
      environment: "enum",
      verify_tls: "boolean",
      labels: "string-array",
    });
  });

  it("treats writeOnly and format:password as secret, and agrees with the server", () => {
    // `writeOnly` is JSON Schema's own vocabulary (draft 2019-09 §9.4). The
    // backend's `secret_keys_of` uses the SAME two rules, and they must agree:
    // the server masks based on its answer and the client decides whether to
    // send the mask back based on this one. A disagreement sends "••••" as a
    // real credential.
    expect(isSecretField({ writeOnly: true })).toBe(true);
    expect(isSecretField({ format: "password" })).toBe(true);
    expect(isSecretField({ type: "string" })).toBe(false);
    expect(secretFieldNames(toFormModel(SCHEMA))).toEqual([
      "api_token",
      "legacy",
    ]);
  });

  it("marks required fields from the schema's required list", () => {
    const model = toFormModel(SCHEMA);
    expect(model.fields.find((f) => f.name === "workspace")?.required).toBe(
      true,
    );
    expect(model.fields.find((f) => f.name === "interval")?.required).toBe(
      false,
    );
  });

  it("labels a field from its title, else unsnakes the property name", () => {
    const model = toFormModel({
      type: "object",
      properties: {
        workspace: { type: "string", title: "Workspace" },
        sync_interval_minutes: { type: "string" },
      },
    });
    expect(model.fields[0].label).toBe("Workspace");
    expect(model.fields[1].label).toBe("Sync interval minutes");
  });

  it("REPORTS a field it cannot render rather than dropping it", () => {
    // The one behaviour that must never regress: an omitted field means a
    // config saved without it.
    const model = toFormModel({
      type: "object",
      properties: {
        ok: { type: "string" },
        nested: { type: "object", properties: {} },
        numbers: { type: "array", items: { type: "number" } },
      },
    });
    expect(model.fields.map((f) => f.name)).toEqual(["ok"]);
    expect([...unsupportedFields(model)].sort()).toEqual(["nested", "numbers"]);
  });

  it("uses a textarea for a very long string", () => {
    const model = toFormModel({
      type: "object",
      properties: { body: { type: "string", maxLength: 4000 } },
    });
    expect(model.fields[0].kind).toBe("textarea");
  });

  it("survives a schema that is not a schema", () => {
    // Manifests are plugin-authored. A render must not throw on rubbish.
    expect(toFormModel(null).fields).toEqual([]);
    expect(toFormModel(undefined).fields).toEqual([]);
    expect(toFormModel({ type: "object" }).fields).toEqual([]);
    expect(toFormModel({ properties: "nope" }).fields).toEqual([]);
    expect(
      toFormModel({ type: "object", properties: { bad: 42 } }).unsupported,
    ).toHaveLength(1);
  });

  it("rejects an enum with no usable values", () => {
    const model = toFormModel({
      type: "object",
      properties: { mode: { type: "string", enum: [] } },
    });
    expect(model.fields).toEqual([]);
    expect(unsupportedFields(model)).toEqual(["mode"]);
  });
});

describe("toZodSchema", () => {
  const schema = toZodSchema(toFormModel(SCHEMA));

  it("accepts a fully populated config", () => {
    expect(
      schema.safeParse({
        workspace: "acme",
        api_token: "a-real-token",
        legacy: "",
        interval: 15,
        environment: "staging",
        verify_tls: true,
        labels: ["a"],
      }).success,
    ).toBe(true);
  });

  it("requires a required field", () => {
    expect(schema.safeParse({ workspace: "" }).success).toBe(false);
  });

  it("lets an optional string be empty rather than failing", () => {
    // Empty means "not set". Treating it as a validation error would make an
    // untouched optional field block the form.
    expect(schema.safeParse({ workspace: "a", legacy: "" }).success).toBe(true);
  });

  it("enforces numeric bounds and coerces the DOM's string", () => {
    // Inputs hand back strings; a naive pass-through produces NaN, which
    // serializes to null and wipes the saved value.
    expect(schema.safeParse({ workspace: "a", interval: "20" }).success).toBe(
      true,
    );
    expect(schema.safeParse({ workspace: "a", interval: "99" }).success).toBe(
      false,
    );
    expect(schema.safeParse({ workspace: "a", interval: "abc" }).success).toBe(
      false,
    );
  });

  it("accepts the SENTINEL for a secret whose schema sets a longer minLength", () => {
    // THE bug this guards: the server masks a saved secret as "••••" (4 chars)
    // and the sample plugin declares minLength 8, so validating the mask like
    // ordinary input rejected a field the operator never touched — making the
    // whole dialog unsubmittable until they retyped the real credential, which
    // is precisely what the mask exists to avoid.
    expect(
      schema.safeParse({ workspace: "a", api_token: SECRET_SENTINEL }).success,
    ).toBe(true);
  });

  it("still enforces minLength on a secret the operator actually typed", () => {
    // The exemption must be for the sentinel ONLY, not for secrets generally —
    // otherwise the schema's constraints stop applying to real input.
    expect(
      schema.safeParse({ workspace: "a", api_token: "short" }).success,
    ).toBe(false);
    expect(
      schema.safeParse({ workspace: "a", api_token: "long-enough-token" })
        .success,
    ).toBe(true);
  });

  it("keeps a value the current schema no longer declares", () => {
    // A plugin upgrade can drop a property while the saved config still holds
    // it. `strict` would make the plugin unconfigurable until someone edited
    // the database by hand.
    expect(
      schema.safeParse({ workspace: "a", removed_last_version: "x" }).success,
    ).toBe(true);
  });
});

describe("initialValues", () => {
  const model = toFormModel(SCHEMA);

  it("prefers the saved value over the schema default", () => {
    expect(initialValues(model, { interval: 5 }).interval).toBe(5);
  });

  it("falls back to the schema default", () => {
    expect(initialValues(model).interval).toBe(15);
    expect(initialValues(model).verify_tls).toBe(true);
  });

  it("gives EVERY field an entry", () => {
    // react-hook-form treats an absent key as an uncontrolled input; one that
    // flips to controlled when data lands loses whatever was typed meanwhile.
    const values = initialValues(model);
    for (const field of model.fields) {
      expect(values).toHaveProperty(field.name);
    }
  });

  it("carries a masked secret through as the saved value", () => {
    // The server sends •••• for a saved secret; the form shows it and submits
    // it back untouched, and the SERVER restores the real value.
    expect(initialValues(model, { api_token: "••••" }).api_token).toBe("••••");
  });
});

describe("toSubmitPayload", () => {
  const model = toFormModel(SCHEMA);

  it("drops an empty optional string rather than persisting one", () => {
    const payload = toSubmitPayload(model, {
      workspace: "acme",
      legacy: "",
      labels: [],
    });
    expect(payload).toEqual({ workspace: "acme" });
  });

  it("KEEPS a masked secret so the server can recognise 'unchanged'", () => {
    // Stripping it here would read as "clear this secret" — the client cannot
    // restore the real value, because it never had it.
    const payload = toSubmitPayload(model, {
      workspace: "acme",
      api_token: "••••",
    });
    expect(payload.api_token).toBe("••••");
  });

  it("CARRIES FORWARD a saved key the form could not render", () => {
    // `configure` REPLACES the stored document, so a key omitted here is
    // deleted. That silently destroyed any field the renderer does not support
    // — and `risk_overrides`, which the supervisor reads and no schema
    // declares, so no form will ever submit it.
    const payload = toSubmitPayload(
      model,
      { workspace: "acme" },
      {
        workspace: "old",
        risk_overrides: { "tool.a": "read" },
        nested: { x: 1 },
      },
    );
    expect(payload.risk_overrides).toEqual({ "tool.a": "read" });
    expect(payload.nested).toEqual({ x: 1 });
    // A key the form DID render still takes the form's value, not the saved one.
    expect(payload.workspace).toBe("acme");
  });

  it("does not resurrect a rendered field the operator cleared", () => {
    // The carry-forward must not fight the form: an optional field emptied on
    // purpose stays empty rather than reverting to its saved value.
    const payload = toSubmitPayload(
      model,
      { workspace: "acme", legacy: "" },
      { workspace: "acme", legacy: "was-set" },
    );
    expect("legacy" in payload).toBe(false);
  });

  it("converts a numeric string back to a number", () => {
    expect(
      toSubmitPayload(model, { workspace: "a", interval: "30" }).interval,
    ).toBe(30);
  });

  it("keeps false, which is a value and not an absence", () => {
    expect(
      toSubmitPayload(model, { workspace: "a", verify_tls: false }).verify_tls,
    ).toBe(false);
  });
});
