import { useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'

interface ReviewPayload {
  feedbackId: number
  action: 'approve' | 'dismiss'
  sessionToken: string
}

// Approve returns what the filer actually did — it can dedupe, rate-limit or error out, so
// "approved" alone does not mean an issue exists.
export interface ReviewResult {
  reviewed: boolean
  action: string
  filing_result?: {
    action?: string
    issue_number?: number
  }
}

export function useFeedbackReview() {
  const queryClient = useQueryClient()
  return useMutation<ReviewResult, unknown, ReviewPayload>({
    mutationFn: ({ feedbackId, action, sessionToken }: ReviewPayload) =>
      api
        .post(`/admin/feedback/${feedbackId}/review`, {
          session_token: sessionToken,
          action,
        })
        .then((r) => r.data.detail),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-feedback'] })
    },
  })
}
