import * as React from "react"
import * as TabsPrimitive from "@radix-ui/react-tabs"
import { motion } from "framer-motion"
import { cn } from "@/lib/utils"

export const Tabs = TabsPrimitive.Root

/** A bottom-rule tab bar, the way product navigation usually reads. */
export const TabsList = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.List
    ref={ref}
    className={cn("flex items-center gap-7 border-b border-border", className)}
    {...props}
  />
))
TabsList.displayName = "TabsList"

export const TabsTrigger = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger> & { active?: boolean }
>(({ className, children, active, ...props }, ref) => (
  <TabsPrimitive.Trigger
    ref={ref}
    className={cn(
      "relative inline-flex items-center gap-2 pb-3 pt-1 text-[13.5px] font-medium transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
      active ? "text-primary" : "text-muted-foreground hover:text-foreground",
      className
    )}
    {...props}
  >
    {children}
    {active && (
      <motion.span
        layoutId="tab-underline"
        transition={{ type: "spring", stiffness: 480, damping: 38 }}
        className="absolute inset-x-0 -bottom-px h-[2px] rounded-full bg-primary"
        aria-hidden="true"
      />
    )}
  </TabsPrimitive.Trigger>
))
TabsTrigger.displayName = "TabsTrigger"

export const TabsContent = TabsPrimitive.Content
