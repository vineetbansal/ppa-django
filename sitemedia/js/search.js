import ReactiveForm from './ReactiveForm'
import Histogram from './Histogram'
import clearable from './clearable'

$(function(){

    /* components */
    const archiveSearchForm = new ReactiveForm('.form')
    const dateHistogram = new Histogram('#histogram')

    /* dom */
    const $$results = $('.ajax-container')
    const $$paginationTop = $('.page-controls').first()
    const $$resultsCount = $('.workscount .count')
    const $$clearDatesLink = $('.clear-selection')
    const $$minDateInput = $('#id_pub_date_0')
    const $$maxDateInput = $('#id_pub_date_1')
    const $$sortDropdown = $('#sort')
    const $$sortInput = $('#sort input')
    const $$collectionInputs = $('#collections input')
    const $$textInputs = $('input[type="text"]')
    const $$relevanceOption = $('#sort .item[data-value="relevance"]')
    const $$advancedSearchButton = $('.show-advanced')

    /* bindings */
    archiveSearchForm.onStateChange(submitForm)
    $$clearDatesLink.click(onClearDates)
    $$collectionInputs.change(onCollectionChange)
    $$advancedSearchButton.click(toggleAdvancedSearch)
    onPageLoad() // misc functions that run once on page load

    $$collectionInputs
        .focus(e => $(e.target).parent().addClass('focus')) // make collection buttons focusable
        .blur(e => $(e.target).parent().removeClass('focus')) 
        .keypress(e => { if (e.which == 13) $(e.target).click() }) // pressing enter "clicks" them
    
    /* functions */
    function submitForm(state) {
        if (!validate()) return // don't submit an invalid form
        state = state.filter(field => field.value != '') // filter out empty fields
        if (state.filter(field => field.name == 'collections').length == 0) { // if the user manually turned off all collections...
            state.push({ name: "collections", value: "" }) // add a blank value to indicate that specific case
        }
        if (state.filter(field => $$textInputs.get().map(el => el.name).includes(field.name)).length == 0) { // if no text query,
            $$relevanceOption.addClass('disabled') // disable relevance
            let sort = state.find(field => field.name == 'sort') // check if a sort was set
            if (sort && sort.value == 'relevance') { // and if it was relevance,
                $$sortDropdown.dropdown('set selected', 'title_asc') // set to title A-Z
            }
        }
        else {
            $$relevanceOption.removeClass('disabled') // enable relevance sort
        }
        let url = `?${$.param(state)}` // serialize state using $.param to make querystring
        window.history.pushState(state, 'PPA Archive Search', url) // update the URL bar
        let req = fetch(`/archive/${url}`, { // create the submission request
            headers: { // this header is needed to signal ajax request to django
                'X-Requested-With': 'XMLHttpRequest'
            }
        })
        req.then(res => res.text()).then(html => { // submit the form and get html back
            $$paginationTop.html($(html).find('.page-controls').html()) // update the top pagination
            dateHistogram.update(JSON.parse($(html).find('pre.facets').html())) // update the histogram
            $$resultsCount.html($(html).find('pre.count').html()) // update the results count
            $$results.html(html) // update the results
            document.dispatchEvent(new Event('ZoteroItemUpdated', { // notify Zotero of changed results
                bubbles: true,
                cancelable: true
            }))
        })
    }

    function validate() {
        if (!$$minDateInput[0].checkValidity() || !$$maxDateInput[0].checkValidity()) {
            $('.validation').css('visibility', 'visible')
            return false
        }
        $('.validation').css('visibility', 'hidden')
        return true
    }

    function onClearDates() {
        $$minDateInput.val('') // clear the date inputs
        $$maxDateInput.val('')
        $$minDateInput[0].dispatchEvent(new Event('input')) // fake input events to trigger resubmit
        $$maxDateInput[0].dispatchEvent(new Event('input'))
    }

    function onCollectionChange(event) {
        $(event.currentTarget).parent().toggleClass('active')
    }

    function onPageLoad() {
        dateHistogram.update(JSON.parse($('.ajax-container pre.facets').html())) // render the histogram initially
        $$collectionInputs.filter(':disabled').parent().addClass('disabled') // disable empty collections
        $('.question-popup').popup() // initialize the question popup
        $$sortDropdown.dropdown('setting', {
            onChange: () => $$sortInput[0].dispatchEvent(new Event('input')) // make sure sort changes trigger a submission
        })
        $('.form').keydown(e => { if (e.which === 13) e.preventDefault() }) // don't allow enter key to submit the search
        $$textInputs.each((_, el) => clearable(el)) // make text inputs clearable
        validate()
    }

    function toggleAdvancedSearch() {
        $('.advanced').slideToggle()
    }
})