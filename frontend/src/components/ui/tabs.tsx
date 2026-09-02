import * as TabsPrimitive from "@radix-ui/react-tabs"

/**
 * Section switching is driven by the sidebar, so only the Root and Content
 * parts are used. The list and triggers were removed rather than left as
 * unused surface area.
 */
export const Tabs = TabsPrimitive.Root
export const TabsContent = TabsPrimitive.Content
