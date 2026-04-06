import { QueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ForbiddenError } from "@/lib/api"

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 30,
      retry: (failureCount, error) => {
        if (error instanceof ForbiddenError) return false
        return failureCount < 1
      },
    },
    mutations: {
      onError: (error) => {
        if (error instanceof ForbiddenError) toast.error(error.message)
      },
    },
  },
})
