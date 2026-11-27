import { useEffect, useRef, useState } from "react"
import { serverError } from "./api"

export function useDatedReport<T>(load: () => Promise<T>) {
  const request = useRef(0)
  const [report, setReport] = useState<{ load: typeof load; data: T | null; err: string | null } | null>(null)

  useEffect(() => {
    const id = ++request.current
    load().then(
      (data) => { if (id === request.current) setReport({ load, data, err: null }) },
      (error) => { if (id === request.current) setReport({ load, data: null, err: serverError(error) }) },
    )
    return () => { ++request.current }
  }, [load])

  // A new request hides the previous result on the same render, before its request starts.
  return report?.load === load ? report : { data: null, err: null }
}
