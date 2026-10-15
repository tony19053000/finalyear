import * as RadixTabs from "@radix-ui/react-tabs";
import type { ReactNode } from "react";

import { cn } from "../lib/cn";

export interface TabItem {
  value: string;
  label: ReactNode;
  content: ReactNode;
}

export interface TabsProps {
  items: TabItem[];
  /** Accessible name of the tab list. */
  label: string;
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
  className?: string;
}

/** Radix Tabs (arrow-key navigation, roving focus, ARIA) in dashboard style. */
export function Tabs({ items, label, value, defaultValue, onValueChange, className }: TabsProps) {
  const rootProps = value !== undefined ? { value } : { defaultValue: defaultValue ?? items[0]?.value };
  return (
    <RadixTabs.Root
      {...rootProps}
      onValueChange={onValueChange}
      className={cn("flex flex-col", className)}
    >
      <RadixTabs.List aria-label={label} className="flex gap-1 border-b border-border">
        {items.map((item) => (
          <RadixTabs.Trigger
            key={item.value}
            value={item.value}
            className={cn(
              "-mb-px h-9 border-b-2 border-transparent px-3 text-sm text-fg-muted",
              "hover:text-fg data-[state=active]:border-accent data-[state=active]:text-fg",
            )}
          >
            {item.label}
          </RadixTabs.Trigger>
        ))}
      </RadixTabs.List>
      {items.map((item) => (
        <RadixTabs.Content key={item.value} value={item.value} className="pt-4">
          {item.content}
        </RadixTabs.Content>
      ))}
    </RadixTabs.Root>
  );
}
