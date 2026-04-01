import { useGuidedPrd } from '../../hooks/useGuidedPrd'
import { FeatureDraftCard } from './FeatureDraftCard'
import { FeatureConfirmCard } from './FeatureConfirmCard'
import { FeatureLockedCard } from './FeatureLockedCard'
import { WriteProgressCard } from './WriteProgressCard'
import { WriteTargetChangeCard } from './WriteTargetChangeCard'
import type { PrdCardData } from '../../types/guided-prd'

interface PrdCardRendererProps {
  card: PrdCardData
  isDone: boolean
  msgId: string
  conversationId: string
}

export function PrdCardRenderer({ card, isDone, msgId, conversationId }: PrdCardRendererProps) {
  const { submitAnswer, confirmFeature, rejectFeature } = useGuidedPrd(conversationId)

  switch (card.type) {
    case 'feature_draft':
      return (
        <FeatureDraftCard
          data={card}
          isDone={isDone}
          msgId={msgId}
          onSubmit={submitAnswer}
        />
      )

    case 'feature_confirm':
      return (
        <FeatureConfirmCard
          data={card}
          isDone={isDone}
          msgId={msgId}
          onConfirm={confirmFeature}
          onReject={rejectFeature}
        />
      )

    case 'feature_locked':
      return <FeatureLockedCard data={card} />

    case 'write_progress':
      return <WriteProgressCard data={card} />

    case 'write_target_change':
      return (
        <WriteTargetChangeCard
          data={card}
          isDone={isDone}
          msgId={msgId}
          conversationId={conversationId}
        />
      )

    default:
      return null
  }
}
