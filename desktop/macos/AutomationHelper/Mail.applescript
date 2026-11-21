on permissionCheck()
    tell application "Mail" to return name
end permissionCheck

on recentMessages(cutoff)
    set cutoffDate to cutoff as date
    with timeout of 20 seconds
    tell application "Mail"
        set candidates to (get messages of inbox whose date received is greater than cutoffDate)
        if (count candidates) > 500 then error "Inbox backlog exceeds the limit" number 5001
        set rows to {}
        repeat with itemMessage in candidates
            set end of rows to {id of itemMessage, message id of itemMessage, subject of itemMessage, date received of itemMessage}
        end repeat
        return rows
    end tell
    end timeout
end recentMessages

on inboxInfo()
    with timeout of 8 seconds
        tell application "Mail" to return {count of accounts, count of messages of inbox}
    end timeout
end inboxInfo

on messageContent(messageNumber, maximumLength)
    tell application "Mail"
        set itemMessage to first message of inbox whose id is messageNumber
        set bodyText to content of itemMessage as text
        set bodyTruncated to (length of bodyText) > maximumLength
        if bodyTruncated then set bodyText to text 1 thru maximumLength of bodyText
        return {subject of itemMessage, bodyText, message id of itemMessage, sender of itemMessage, bodyTruncated}
    end tell
end messageContent

on saveReply(messageNumber, expectedMessageID, replyText)
    with timeout of 20 seconds
    tell application "Mail"
        set itemMessage to first message of inbox whose id is messageNumber
        if (message id of itemMessage) is not expectedMessageID then error "Message changed" number 5002
        set previousDrafts to id of every message of drafts mailbox
        set ownAddresses to email addresses of account of mailbox of itemMessage
        if (count ownAddresses) is 0 then error "No sending address for this account" number 5002
        set replySender to item 1 of ownAddresses
        set originalRecipients to address of every recipient of itemMessage
        repeat with ownAddress in ownAddresses
            if originalRecipients contains (ownAddress as text) then
                set replySender to ownAddress as text
                exit repeat
            end if
        end repeat
        set replyMessage to reply itemMessage with opening window
        set sender of replyMessage to replySender
        if (extract address from (sender of replyMessage)) is not replySender then error "Reply account changed" number 5002
        -- Let Mail finish initializing the composer before setting its body.
        -- Otherwise the asynchronous quote setup can overwrite our reply.
        delay 1
        set content of replyMessage to replyText
        save replyMessage
        set draftNumber to my confirmedDraft(replyMessage, previousDrafts, replyText)
        close replyMessage saving yes
        return draftNumber
    end tell
    end timeout
end saveReply

on confirmedDraft(replyMessage, previousDrafts, replyText)
    tell application "Mail"
        set replySubject to subject of replyMessage
        set replySender to extract address from (sender of replyMessage)
        set replyTo to address of every to recipient of replyMessage
        set replyCC to address of every cc recipient of replyMessage
        set replyBCC to address of every bcc recipient of replyMessage
        -- Save is asynchronous. The outgoing-message id is temporary (often
        -- zero), and closing immediately can discard the unsaved reply.
        -- Confirm a new, matching Drafts record before releasing the composer.
        repeat 60 times
            set candidates to (get messages of drafts mailbox whose subject is replySubject)
            repeat with candidate in candidates
                set draftNumber to id of candidate
                if draftNumber > 0 and previousDrafts does not contain draftNumber and subject of candidate is replySubject then
                    set matchesText to my replyBodyMatches(content of candidate as text, replyText)
                    if matchesText and (extract address from (sender of candidate)) is replySender and (address of every to recipient of candidate) is replyTo and (address of every cc recipient of candidate) is replyCC and (address of every bcc recipient of candidate) is replyBCC then
                        return draftNumber
                    end if
                end if
            end repeat
            delay 0.25
        end repeat
        -- Keep an unconfirmed composer intact for review; never blindly retry.
        error "Mail has not confirmed the saved draft" number 5003
    end tell
end confirmedDraft

-- Mail's MIME serialization adds blank lines and trailing whitespace. Ignore
-- only that formatting; keep paragraph order and all substantive text exact.
on normalizedReplyParagraphs(value)
    set resultParagraphs to {}
    set padding to {space, tab, character id 160}
    repeat with p in paragraphs of value
        set lineText to p as text
        repeat while lineText is not "" and padding contains first character of lineText
            if length of lineText is 1 then
                set lineText to ""
            else
                set lineText to text 2 thru -1 of lineText
            end if
        end repeat
        repeat while lineText is not "" and padding contains last character of lineText
            if length of lineText is 1 then
                set lineText to ""
            else
                set lineText to text 1 thru -2 of lineText
            end if
        end repeat
        if lineText is not "" then set end of resultParagraphs to lineText
    end repeat
    return resultParagraphs
end normalizedReplyParagraphs

on replyBodyMatches(actualText, expectedText)
    considering case
        return my normalizedReplyParagraphs(actualText) is my normalizedReplyParagraphs(expectedText)
    end considering
end replyBodyMatches

on recoverReply(messageNumber, expectedMessageID, replyText, notBefore)
    with timeout of 20 seconds
    tell application "Mail"
        set originalMessage to first message of inbox whose id is messageNumber
        if message id of originalMessage is not expectedMessageID then error "Original changed" number 5002
        set ownAddresses to email addresses of account of mailbox of originalMessage
        set destination to extract address from (reply to of originalMessage)
        if destination is "" then set destination to extract address from (sender of originalMessage)
        set foundDraft to 0
        set candidates to (get messages of drafts mailbox whose date sent is greater than or equal to notBefore)
        repeat with candidate in candidates
            if date sent of candidate is greater than or equal to notBefore and (all headers of candidate) contains expectedMessageID then
                if ownAddresses contains (extract address from (sender of candidate)) and (address of every to recipient of candidate) is {destination} and (count of cc recipients of candidate) is 0 then
                    if my replyBodyMatches(content of candidate as text, replyText) then
                        -- Multiple matching copies need review, not a guess.
                        if foundDraft is not 0 then return 0
                        set foundDraft to id of candidate
                    end if
                end if
            end if
        end repeat
        return foundDraft
    end tell
    end timeout
end recoverReply

on draftDetails(draftNumber)
    tell application "Mail"
        set itemMessage to first message of drafts mailbox whose id is draftNumber
        if (id of itemMessage) is not draftNumber then error "Draft unavailable" number 5004
        return {id of itemMessage, message id of itemMessage, subject of itemMessage, sender of itemMessage, address of every to recipient of itemMessage, address of every cc recipient of itemMessage, address of every bcc recipient of itemMessage, content of itemMessage as text, count of mail attachments of itemMessage}
    end tell
end draftDetails

on sendReviewedDraft(draftNumber, originalNumber, originalMessageID, expectedSnapshot)
    with timeout of 20 seconds
        set currentSnapshot to my draftDetails(draftNumber)
        if currentSnapshot is not expectedSnapshot then error "Draft changed since preview" number 5004
        -- The generated replies are plain text. Preserve any attachments added
        -- later by leaving those drafts for Mail's own compose window.
        if item 9 of currentSnapshot is not 0 then error "Send attachments in Mail" number 5005
        tell application "Mail"
            set originalMessage to first message of inbox whose id is originalNumber
            if message id of originalMessage is not originalMessageID then error "Original message changed" number 5004
            set previousDrafts to id of every message of drafts mailbox
            -- A stored Drafts message cannot receive Mail's send command.
            -- Rebuild it as a reply to the same original, preserving threading.
            set outgoingReply to reply originalMessage with opening window
            set sender of outgoingReply to item 4 of currentSnapshot
            set subject of outgoingReply to item 3 of currentSnapshot
            tell outgoingReply
                delete every to recipient
                delete every cc recipient
                delete every bcc recipient
                repeat with destination in item 5 of currentSnapshot
                    make new to recipient at end of to recipients with properties {address:destination as text}
                end repeat
                repeat with destination in item 6 of currentSnapshot
                    make new cc recipient at end of cc recipients with properties {address:destination as text}
                end repeat
                repeat with destination in item 7 of currentSnapshot
                    make new bcc recipient at end of bcc recipients with properties {address:destination as text}
                end repeat
            end tell
            delay 1
            set content of outgoingReply to item 8 of currentSnapshot
            save outgoingReply
            set confirmedNumber to my confirmedDraft(outgoingReply, previousDrafts, item 8 of currentSnapshot)
            if (extract address from (sender of outgoingReply)) is not (extract address from (item 4 of currentSnapshot)) then error "Sender changed" number 5004
            if (address of every to recipient of outgoingReply) is not (item 5 of currentSnapshot) then error "Recipients changed" number 5004
            if (address of every cc recipient of outgoingReply) is not (item 6 of currentSnapshot) then error "Recipients changed" number 5004
            if (address of every bcc recipient of outgoingReply) is not (item 7 of currentSnapshot) then error "Recipients changed" number 5004
            if my draftDetails(draftNumber) is not expectedSnapshot then error "Draft changed since preview" number 5004
            set sendStarted to current date
            try
                send outgoingReply
            end try
            -- Mail may queue a send (including its Undo Send delay) without
            -- returning true. Verify the exact reply in Sent or Outbox instead
            -- of treating that asynchronous acknowledgement as failure.
            set deadline to (current date) + 30
            set confirmedSend to false
            repeat while (current date) < deadline
                if my sentReplyExists(currentSnapshot, originalMessageID, sendStarted) then
                    set confirmedSend to true
                    exit repeat
                end if
                delay 1
            end repeat
            if not confirmedSend then error "Mail did not confirm the reply in Sent or Outbox" number 5006
            -- Consume only the reviewed draft, and only after Mail accepted
            -- sending. Cleanup failure must never cause a repeated send.
            try
                if my draftDetails(draftNumber) is expectedSnapshot then delete (first message of drafts mailbox whose id is draftNumber)
            end try
            return true
        end tell
    end timeout
end sendReviewedDraft

on sentReplyExists(expectedSnapshot, originalMessageID, sendStarted)
    tell application "Mail"
        set recentSent to (get messages of sent mailbox whose date sent is greater than or equal to sendStarted)
        set candidates to recentSent & (get messages of outbox)
        repeat with candidate in candidates
            if date sent of candidate is greater than or equal to sendStarted and subject of candidate is item 3 of expectedSnapshot and (all headers of candidate) contains originalMessageID then
                if (extract address from (sender of candidate)) is (extract address from (item 4 of expectedSnapshot)) and (address of every to recipient of candidate) is item 5 of expectedSnapshot and (address of every cc recipient of candidate) is item 6 of expectedSnapshot then
                    if my replyBodyMatches(content of candidate as text, item 8 of expectedSnapshot) then return true
                end if
            end if
        end repeat
    end tell
    return false
end sentReplyExists
