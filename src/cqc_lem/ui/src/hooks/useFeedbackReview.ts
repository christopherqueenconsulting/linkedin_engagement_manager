import { useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'

interface ReviewPayload {
  feedbackId: number
  action: 'approve' | 'dismiss'
  sessionToken: string
}

export function useFeedbackReview() {
  const queryClient = useQueryClient()
  return useMutation({
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
