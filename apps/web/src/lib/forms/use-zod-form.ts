"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import {
  useForm,
  type UseFormProps,
  type UseFormReturn,
} from "react-hook-form";
import type { TypeOf, ZodType } from "zod";

/**
 * The shared form engine (Vol 4 §4.1): react-hook-form wired to a zod schema,
 * with the value type inferred from the schema. Screens define a zod schema —
 * ideally derived from the generated client types — and get typed fields,
 * validation, and errors for free.
 */
export function useZodForm<S extends ZodType>(
  schema: S,
  options?: Omit<UseFormProps<TypeOf<S>>, "resolver">,
): UseFormReturn<TypeOf<S>> {
  return useForm<TypeOf<S>>({
    ...options,
    resolver: zodResolver(schema),
  });
}
