import { Link } from "@tanstack/react-router"
import { Bot } from "lucide-react"

import { cn } from "@/lib/utils"

interface LogoProps {
  variant?: "full" | "icon" | "responsive"
  className?: string
  asLink?: boolean
}

export function Logo({
  variant = "full",
  className,
  asLink = true,
}: LogoProps) {
  const content =
    variant === "responsive" ? (
      <>
        <span
          className={cn(
            "text-xl font-semibold tracking-tight group-data-[collapsible=icon]:hidden",
            className,
          )}
        >
          AgentHub
        </span>
        <Bot
          aria-label="AgentHub"
          className={cn(
            "size-5 hidden group-data-[collapsible=icon]:block",
            className,
          )}
        />
      </>
    ) : variant === "full" ? (
      <span className={cn("text-xl font-semibold tracking-tight", className)}>
        AgentHub
      </span>
    ) : (
      <Bot aria-label="AgentHub" className={cn("size-5", className)} />
    )

  if (!asLink) {
    return content
  }

  return <Link to="/">{content}</Link>
}
