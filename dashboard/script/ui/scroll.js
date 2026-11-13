import { hideTip } from './tooltip.js';

/** A tooltip should never be left floating after the page scrolls from under the pointer. */
export default function hideTipOnScroll(scroller) {
    scroller.addEventListener('scroll', hideTip, { passive: true });
}
