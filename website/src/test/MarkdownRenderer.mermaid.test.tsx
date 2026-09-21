import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/react'

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({ svg: '<svg></svg>' }),
  },
}))

import mermaid from 'mermaid'
import MarkdownRenderer from '../components/MarkdownRenderer'

beforeEach(() => { vi.clearAllMocks() })

describe('MarkdownRenderer mermaid config', () => {
  it('initializes mermaid with suppressErrorRendering so parse errors do not leak error SVGs into the DOM', async () => {
    // Regression: without suppressErrorRendering, a mermaid parse error injects a
    // temp <div id="dmermaid-*"> into document.body that render() never cleans up
    // (cleanup only runs on success), accumulating orphaned error graphics.
    //
    // Awaited because mermaid is loaded by `import()` inside MermaidBlock
    // (it is ~90-130 KB gzip and must stay off the critical path), so
    // initialize() lands a microtask after render rather than during it.
    render(<MarkdownRenderer content={'```mermaid\ngraph TD;A-->B\n```'} />)
    await vi.waitFor(() =>
      expect(mermaid.initialize).toHaveBeenCalledWith(
        expect.objectContaining({ suppressErrorRendering: true })
      )
    )
  })

  it('renders the diagram through the lazily-imported module', async () => {
    render(<MarkdownRenderer content={'```mermaid\ngraph TD;A-->B\n```'} />)
    await vi.waitFor(() => expect(mermaid.render).toHaveBeenCalled())
  })

  it('loads the rendered label glyphs and waits for fonts before measuring the diagram', async () => {
    let releaseReady!: () => void
    const ready = new Promise<void>(resolve => { releaseReady = resolve })
    const load = vi.fn().mockResolvedValue([])
    const original = Object.getOwnPropertyDescriptor(document, 'fonts')
    Object.defineProperty(document, 'fonts', {
      configurable: true,
      value: { load, ready },
    })
    try {
      render(<MarkdownRenderer content={'```mermaid\ngraph TD;A["節点 label"]-->B\n```'} />)
      await vi.waitFor(() => expect(load).toHaveBeenCalled())
      expect(mermaid.render).not.toHaveBeenCalled()

      releaseReady()
      await vi.waitFor(() => expect(mermaid.render).toHaveBeenCalledTimes(1))
      expect(load.mock.calls[0][1]).toContain('節点 label')
      expect(mermaid.initialize).toHaveBeenCalledWith(
        expect.objectContaining({ fontFamily: expect.not.stringMatching(/^inherit$/i) })
      )
    } finally {
      if (original) Object.defineProperty(document, 'fonts', original)
      else delete (document as Document & { fonts?: FontFaceSet }).fonts
    }
  })

  it('does NOT touch mermaid for content without a diagram', async () => {
    // The point of the dynamic import: mermaid must not be pulled in — nor
    // initialized — just because a chat message rendered.
    render(<MarkdownRenderer content={'# Hello\n\nplain text and `code`'} />)
    await new Promise(r => setTimeout(r, 50))
    expect(mermaid.initialize).not.toHaveBeenCalled()
    expect(mermaid.render).not.toHaveBeenCalled()
  })
})
