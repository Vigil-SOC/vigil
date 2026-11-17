// A case id already says "case-…"; only an id without that prefix needs the word.
export const caseLabel = (id: string): string => (id.startsWith('case-') ? id : `Case ${id}`)
