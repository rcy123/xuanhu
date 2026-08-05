import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { RequestMoreInfoModal } from './RequestMoreInfoModal'

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('RequestMoreInfoModal', () => {
  it('requires supplemental information before restarting reasoning', () => {
    const onSubmit = vi.fn()
    render(
      <RequestMoreInfoModal
        open={true}
        submitting={false}
        onCancel={vi.fn()}
        onSubmit={onSubmit}
      />,
    )

    expect(screen.getByTestId('request-more-info-submit-btn')).toBeDisabled()
    fireEvent.change(screen.getByTestId('request-more-info-feedback'), {
      target: { value: '请补充舌象和脉象' },
    })
    fireEvent.click(screen.getByTestId('request-more-info-submit-btn'))
    expect(onSubmit).toHaveBeenCalledWith('请补充舌象和脉象')
  })
})
